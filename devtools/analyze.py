import json,sys,os
# Static candidate check (no tests run): for each sample commit, the candidate
# set, whether it contains every failing test, and why.
# Usage: RTS_REPO=<sqlglot checkout> RTS_MAP=<map.json> python devtools/analyze.py sample/answers.json
HERE=os.path.dirname(os.path.abspath(__file__)); ROOT=os.path.dirname(HERE)
os.environ.setdefault("RTS_DATA",os.path.join(ROOT,"sample"))
sys.path.insert(0,ROOT)
import selector as S
sel=S.Selector()
ans=json.load(open(sys.argv[1]))["commits"]
tot=0
for cid in sorted(ans):
    c,nt,why=sel.candidates(open(os.path.join(os.environ["RTS_DATA"],"commits",f"{cid}.patch")).read())
    f=set(ans[cid]["failing"]); miss=f-c
    cs=sum(sel.ms[n] for n in c)/1000
    heavy=[sel.tests[n]["id"].split("::")[-1] for n in c if sel.ms[n]>=2000]
    print(f"{cid} {ans[cid]['kind']:8} fail={len(f):2} cand={len(c):4} {cs:6.1f}s MISS={len(miss)} heavy={heavy} | {why}")
