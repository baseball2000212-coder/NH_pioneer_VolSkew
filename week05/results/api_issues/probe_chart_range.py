"""NH 해외_차트 API 오류 제보 재현 (조회만, 주문 없음). 실행: week05 에서 NHFUTURES_CONFIG 지정 후 python results/api_issues/probe_chart_range.py"""
import sys, json, time
from pathlib import Path
WEEK05 = Path(__file__).resolve().parents[2]  # week05/
sys.path.insert(0, str(WEEK05))
from nhapi import Client, Journal, load_config
cl = Client(load_config(), journal=Journal(WEEK05 / "logs" / "probe-1120d.jsonl", "probe-1120d", mode="smoke"))
M = "overseas_chart-minute"; D = "overseas_chart-daily"
def call(api, body):
    time.sleep(0.35)
    b = cl.call(api, body).body
    rows = b.get("Occurs1", []) or []
    return b, rows
def key(r): return r.get("occurs1_ref_dt","") + r.get("occurs1_exch_tm","")
R = {}
base = {"exch_cd":"FCME","sym":"MESZ26"}
# 1) 요청 구간 데이터가 실제로 있는지: nxt_key 로 11/19 12:00 이전까지 넘기며 12:00~13:00 봉 수 세기
nk = "99999999"; got = {}; pages = 0
while pages < 8:
    b, rows = call(M, dict(base, dcnt="1", req_qty="495", nxt_key=nk)); pages += 1
    for r in rows: got[key(r)] = r
    nk = b.get("nxt_key")
    if min(key(r) for r in rows) < "20261119115900": break
win = sorted(k for k in got if "20261119120000" <= k <= "20261119130000")
R["exists_1119_12_13"] = {"pages": pages, "count": len(win), "first": win[:1], "last": win[-1:]}
R["ref_dt_vs_glb"] = [(k, got[k]["occurs1_glb_biz_dt"]) for k in win[:1]]
# 2) 기간 요청 변형
var = {
 "v1_range_only": dict(base, inq_strt_dt="20261119", strt_tm="120000", inq_ed_dt="20261119", ed_tm="130000"),
 "v2_range_dcnt1": dict(base, inq_strt_dt="20261119", strt_tm="120000", inq_ed_dt="20261119", ed_tm="130000", dcnt="1"),
 "v3_range_req9999": dict(base, inq_strt_dt="20261119", strt_tm="120000", inq_ed_dt="20261119", ed_tm="130000", dcnt="1", req_qty="9999"),
 "v4_start_only": dict(base, inq_strt_dt="20261119", strt_tm="120000", dcnt="1", req_qty="10"),
 "v5_end_only": dict(base, inq_ed_dt="20261119", ed_tm="130000", dcnt="1", req_qty="10"),
 "v6_date_no_time": dict(base, inq_strt_dt="20261119", inq_ed_dt="20261119", dcnt="1", req_qty="10"),
 "v7_req10_dcnt1": dict(base, dcnt="1", req_qty="10"),
 "v8_req9999_dcnt1": dict(base, dcnt="1", req_qty="9999"),
 "v9_6E_range_req10": {"exch_cd":"FCME","sym":"6EZ26","inq_strt_dt":"20261119","strt_tm":"120000","inq_ed_dt":"20261119","ed_tm":"130000","dcnt":"1","req_qty":"10"},
}
for k, body in var.items():
    try:
        b, rows = call(M, body); ks = [key(r) for r in rows]
        R[k] = {"n": len(rows), "nrec": b.get("nrec"), "min": min(ks) if ks else None, "max": max(ks) if ks else None}
    except Exception as ex:
        R[k] = {"err": f"{getattr(ex,'code','')} {str(ex)[:150]}"}
# 3) 일봉 10/01~10/10
b, rows = call(D, dict(base, inq_strt_dt="20261001", inq_ed_dt="20261010", req_qty="300"))
ds = sorted(r["occurs1_ref_dt"] for r in rows)
R["daily_1001_1010"] = {"n": len(ds), "in_range": sum("20261001" <= d <= "20261010" for d in ds), "out_range": sum(d > "20261010" for d in ds), "min": ds[0], "max": ds[-1]}
b, rows = call(D, dict(base, inq_strt_dt="20261101", inq_ed_dt="20261105", req_qty="300"))
ds = sorted(r["occurs1_ref_dt"] for r in rows)
R["daily_1101_1105"] = {"n": len(ds), "in_range": sum("20261101" <= d <= "20261105" for d in ds), "min": ds[0], "max": ds[-1]}
Path(__file__).with_name("probe_chart_range_1120.json").write_text(json.dumps(R, ensure_ascii=False, indent=1))
print(json.dumps(R, ensure_ascii=False, indent=1))
