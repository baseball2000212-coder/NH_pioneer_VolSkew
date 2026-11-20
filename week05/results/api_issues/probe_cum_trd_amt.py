"""NH 해외_차트 API 오류 제보 재현 (조회만, 주문 없음). 실행: week05 에서 NHFUTURES_CONFIG 지정 후 python results/api_issues/probe_cum_trd_amt.py"""
import sys, json, time
from pathlib import Path
WEEK05 = Path(__file__).resolve().parents[2]  # week05/
sys.path.insert(0, str(WEEK05))
from nhapi import Client, Journal, load_config
cl = Client(load_config(), journal=Journal(WEEK05 / "logs" / "probe-1120c.jsonl", "probe-1120c", mode="smoke"))
def call(api, body):
    time.sleep(0.4)
    try:
        b = cl.call(api, body).body
        return b, None
    except Exception as ex:
        return {}, f"{type(ex).__name__} {getattr(ex,'code','')} {str(ex)[:160]}"
def span(b):
    rows = b.get("Occurs1", []) or []
    if isinstance(rows, dict): rows = [rows]
    ks = [r.get("occurs1_ref_dt","")+r.get("occurs1_exch_tm","") for r in rows]
    return {"n": len(rows), "nrec": b.get("nrec"), "nxt_key": b.get("nxt_key"), "min": min(ks) if ks else None, "max": max(ks) if ks else None}
R = {}
M = "overseas_chart-minute"; D = "overseas_chart-daily"
base = {"exch_cd": "FCME", "sym": "MESZ26"}
tests = {
 "m_minimal": (M, dict(base)),
 "m_dcnt5_req10": (M, dict(base, dcnt="5", req_qty="10")),
 "m_dcnt1_req10": (M, dict(base, dcnt="1", req_qty="10")),
 "m_range_1119_12_13_no_nxt": (M, dict(base, inq_strt_dt="20261119", strt_tm="120000", inq_ed_dt="20261119", ed_tm="130000", dcnt="1", req_qty="300")),
 "m_range_1119_12_13_nxt9": (M, dict(base, inq_strt_dt="20261119", strt_tm="120000", inq_ed_dt="20261119", ed_tm="130000", dcnt="1", req_qty="300", nxt_key="99999999")),
 "m_range_1001_1002": (M, dict(base, inq_strt_dt="20261001", strt_tm="000000", inq_ed_dt="20261002", ed_tm="235959", dcnt="1", req_qty="300", nxt_key="99999999")),
 "d_minimal": (D, dict(base)),
 "d_req10": (D, dict(base, req_qty="10")),
 "d_range_1001_1010": (D, dict(base, inq_strt_dt="20261001", inq_ed_dt="20261010", req_qty="300")),
 "m_MGC_range": (M, {"exch_cd":"FCME","sym":"MGCZ26","inq_strt_dt":"20261119","strt_tm":"120000","inq_ed_dt":"20261119","ed_tm":"130000","dcnt":"1","req_qty":"10","nxt_key":"99999999"}),
 "m_6E_range": (M, {"exch_cd":"FCME","sym":"6EZ26","inq_strt_dt":"20261119","strt_tm":"120000","inq_ed_dt":"20261119","ed_tm":"130000","dcnt":"1","req_qty":"10","nxt_key":"99999999"}),
}
for k, (api, body) in tests.items():
    b, err = call(api, body)
    R[k] = {"body": body, "err": err, "span": span(b) if not err else None}
    if k in ("m_minimal", "m_dcnt5_req10"):
        rows = b.get("Occurs1", [])[:3]; R[k]["first_rows"] = [(r["occurs1_exch_tm"], r["occurs1_cum_trd_qty"]) for r in rows]
# cum_trd_amt: 단위·잘림 (MES, MNQ, MGC, 6E 최신 495줄)
amt = {}
for sym in ("MESZ26", "MNQZ26", "MGCZ26", "6EZ26"):
    b, err = call(M, {"exch_cd":"FCME","sym":sym,"dcnt":"1","req_qty":"300","nxt_key":"99999999"})
    if err: amt[sym] = err; continue
    rows = sorted(b["Occurs1"], key=lambda r: r["occurs1_ref_dt"]+r["occurs1_exch_tm"])
    seq = [(r["occurs1_ref_dt"]+r["occurs1_exch_tm"], int(r["occurs1_cum_trd_amt"]), int(r["occurs1_cum_trd_qty"]), float(r["occurs1_close_pric"])) for r in rows]
    out = []
    for a, c in zip(seq, seq[1:]):
        if c[2] == 0: continue
        r = (c[1]-a[1]) / (c[2]*c[3])
        out.append((c[0], a[1], c[1], c[2], c[3], round(r, 5)))
    # 구간 경계(비율이 10배 바뀌거나 값이 떨어진 곳) 앞뒤
    marks = [i for i in range(1, len(out)) if out[i][2] < out[i][1] or (out[i-1][5] > 0 and out[i][5] > 0 and not (0.5 < out[i][5]/out[i-1][5] < 2))]
    amt[sym] = {"n": len(seq), "maxlen": max(len(str(x[1])) for x in seq), "first": out[:2], "last": out[-2:],
                "marks": [out[max(0,i-1):i+2] for i in marks][:6]}
R["amt"] = amt
Path(__file__).with_name("probe_cum_trd_amt_1120.json").write_text(json.dumps(R, ensure_ascii=False, indent=1, default=str))
for k, v in R.items():
    print(k, json.dumps(v, ensure_ascii=False, default=str)[:1500])
