"""1단계: 업로드된 용봉동 그래프에서 현재 라우팅 코드의 기준 성능 측정.
실제 GPS 사용자가 아니라 고정 좌표/격자 표본이다. 알고리즘은 변경하지 않는다.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
import time

import networkx as nx
from src.recommend.graph_loop_engine import Settings, LoopEngine, load_light_graph, RouteFailure, peak_rss_mb


def check_route(graph, route):
    nodes, keys, path = route['node_ids'], route['edge_ids'], route['path']
    assert len(nodes) == len(keys) + 1
    assert nodes[0] == nodes[-1] and path[0] == path[-1]
    assert len(set(nodes[:-1])) == len(nodes)-1
    tokens=[]
    length=0.0
    geometry=[]
    for a,b,key in zip(nodes,nodes[1:],keys):
        edge=graph[a][b][key]
        tokens.append((min(a,b),max(a,b),key))
        length+=edge['length']
        coords=edge['coords'] if edge['geom_from']==a else list(reversed(edge['coords']))
        for p in coords:
            if not geometry or list(p)!=geometry[-1]:
                geometry.append(list(p))
    assert len(set(tokens))==len(tokens)
    assert abs(length-route['distance_m'])<.011
    assert geometry == path
    assert abs(route['distance_error_m']-abs(length-route['requested_distance_m']))<.011
    return {'closed':True,'no_reused_edges':True,'no_reused_internal_nodes':True,
            'distance_sum_correct':True,'geometry_order_correct':True}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--map',type=Path,required=True)
    parser.add_argument('--out',type=Path,default=Path('running-stage1'))
    args=parser.parse_args()
    args.out.mkdir(parents=True,exist_ok=True)
    settings=Settings(args.map)  # 5초/32후보/스냅 200m 등 실제 기본값 유지
    started=time.perf_counter()
    graph=load_light_graph(settings)
    load_ms=(time.perf_counter()-started)*1000
    engine=LoopEngine(graph,settings)
    latitudes=[d['lat'] for _,d in graph.nodes(data=True)]
    longitudes=[d['lng'] for _,d in graph.nodes(data=True)]
    south,north,west,east=min(latitudes),max(latitudes),min(longitudes),max(longitudes)
    starts=[{'id':'A','label':'기존 표본 A','lat':35.180,'lng':126.900},
            {'id':'B','label':'기존 표본 B','lat':35.179,'lng':126.895},
            {'id':'C','label':'기존 표본 C','lat':35.183,'lng':126.905},
            {'id':'D','label':'기존 표본 D','lat':35.175,'lng':126.910}]
    for row,(fy,vertical) in enumerate([(0.85,'북'),(.50,'중앙'),(.15,'남')]):
        for col,(fx,horizontal) in enumerate([(.15,'서'),(.50,'중앙'),(.85,'동')]):
            starts.append({'id':f'G{row*3+col+1}','label':f'격자 {vertical}·{horizontal}',
                           'lat':south+(north-south)*fy,'lng':west+(east-west)*fx})
    # 순환 불가능한 출발점의 원인을 판별: 단순화 무방향 2-vertex-connected 블록에 포함되는지.
    # 다중 간선 자체로 생기는 2-edge cycle은 별도로 포함한다.
    cycle_nodes=set()
    for component in nx.biconnected_components(nx.Graph(graph)):
        if len(component)>=3:
            cycle_nodes.update(component)
    for u,v in graph.edges():
        if graph.number_of_edges(u,v)>1:
            cycle_nodes.update((u,v))
    for start in starts:
        node,snap=engine.nearest_node(start['lat'],start['lng'])
        start.update(node_id=node,snap_m=round(snap,2),degree=graph.degree(node),
                     snapped_lat=graph.nodes[node]['lat'],snapped_lng=graph.nodes[node]['lng'],
                     node_is_on_cycle=node in cycle_nodes)
    records=[]
    for start in starts:
        for target in [3000,5000,10000]:
            record={'id':f"{start['id']}-{target}",'start_id':start['id'],'target_m':target}
            t=time.perf_counter()
            try:
                route=engine.find_loop(start['lat'],start['lng'],target)
                validation=check_route(graph,route)
                error=route['distance_error_m']/target
                record.update(status='pass' if route['within_tolerance'] else 'distance_mismatch',
                              within_10_percent=error<=.10,route=route,validation=validation)
            except RouteFailure as exc:
                record.update(status='no_route',code=exc.code,message=exc.message)
            record['wall_ms']=round((time.perf_counter()-t)*1000,2)
            records.append(record)
            print(record['id'],record['status'],record.get('route',{}).get('distance_m'),flush=True)
    controls=[]
    leaves=sorted(n for n in graph if graph.degree(n)==1)
    tests=[('outside_map',37.5665,126.9780,'outside_map')]
    if leaves:
        d=graph.nodes[leaves[len(leaves)//2]]
        tests.append(('dead_end',d['lat'],d['lng'],'no_loop_at_start'))
    for kind,lat,lng,expected in tests:
        try:
            engine.find_loop(lat,lng,3000)
            raise AssertionError(f'{kind}: unexpectedly returned a route')
        except RouteFailure as exc:
            assert exc.code==expected
            controls.append({'case':kind,'expected':expected,'actual':exc.code,'passed':True})
    summary=[]
    for target in [3000,5000,10000]:
        subset=[r for r in records if r['target_m']==target]
        counts=Counter(r['status'] for r in subset)
        returned=[r for r in subset if 'route' in r]
        summary.append({'target_m':target,'total':len(subset),'pass_15_percent':counts['pass'],
                        'pass_10_percent':sum(r.get('within_10_percent',False) for r in subset),
                        'distance_mismatch':counts['distance_mismatch'],'no_route':counts['no_route'],
                        'median_ms':round(statistics.median(r['wall_ms'] for r in subset),1),
                        'max_ms':max(r['wall_ms'] for r in subset),
                        'max_error_percent':round(max((r['route']['distance_error_m']/target*100 for r in returned),default=0),2)})
    # 목표 거리가 커졌을 때 이미 찾았던 유효 경로보다 나빠지는지 확인한다.
    dominated=[]
    for current in records:
        alternatives=[r for r in records if r['start_id']==current['start_id'] and 'route' in r]
        if not alternatives:
            continue
        best_known=min(alternatives,key=lambda r:abs(r['route']['distance_m']-current['target_m']))
        known_error=abs(best_known['route']['distance_m']-current['target_m'])
        current_error=current.get('route',{}).get('distance_error_m',float('inf'))
        if known_error + .02 < current_error:
            dominated.append({'case':current['id'],'best_known_case':best_known['id'],
                              'current_distance_m':current.get('route',{}).get('distance_m'),
                              'known_valid_distance_m':best_known['route']['distance_m']})
    report={'stage':'1A: fixed-sample route quality baseline','created_at':datetime.now(timezone.utc).isoformat(),
            'input_file':args.map.name,'map_sha256':hashlib.sha256(args.map.read_bytes()).hexdigest(),
            'router_sha256':hashlib.sha256(Path(__file__).parents[1].joinpath('src/recommend/graph_loop_engine.py').read_bytes()).hexdigest(),
            'limits':{'timeout_s':settings.timeout_s,'max_candidates':settings.max_candidates,
                      'snap_max_m':settings.max_snap_m,'distance_tolerance':.15},
            'graph':{'nodes':len(graph),'edges':graph.number_of_edges(),'components':nx.number_connected_components(graph),
                     'max_edge_m':max(d['length'] for *_,d in graph.edges(data=True)),
                     'leaf_nodes':len(leaves),'cycle_eligible_nodes':len(cycle_nodes),
                     'load_ms':round(load_ms,2),'peak_rss_mib':peak_rss_mb(),
                     'node_bbox':[west,south,east,north]},
            'sampling':'고정 출발 좌표 4개 + 노드 bbox의 15/50/85% 격자 9개. 실제 GPS·무작위 사용자 표본 아님.',
            'summary':summary,'starts':starts,'records':records,'negative_controls':controls,
            'dominated_results':dominated,
            'caveats':['샌드박스 측정으로 Render 성능을 보장하지 않음.',
                       '실제 통행 가능성·보행 안전·GPS·음성 안내는 별도 확인이 필요함.',
                       '순환 경로 존재와 휴리스틱의 발견 가능성은 다름. 지도 크기만을 실패 원인으로 단정하지 않음.',
                       '사용자 좌표에서 스냅 노드까지의 접근 경로는 계산하거나 검증하지 않음.']}
    (args.out/'report.json').write_text(json.dumps(report,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    # 확인 화면용 공개 도로망. 실시간 위치 수집이나 외부 서비스 전송은 하지 않는다.
    context={'roads':[[list(p) for p in d['coords']] for *_,d in graph.edges(data=True)],
             'nodes':[[d['lat'],d['lng'],bool(d.get('crossing'))] for _,d in graph.nodes(data=True)]}
    (args.out/'map_context.json').write_text(json.dumps(context,separators=(',',':')),encoding='utf-8')
    print(json.dumps({'summary':summary,'graph':report['graph'],'controls':controls},ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
