"""效果验证：把 10 身测试搭配在几条换装流程上各跑几次，结果拿到打分页里打分。

    python bench.py --pipelines bailian,kling --runs 1      # 第一轮：每条流程每身跑 1 次，挑出最好的
    python bench.py --pipelines bailian --runs 3            # 第二轮：胜出的流程每身跑 3 次，看稳定性
    python bench.py --mock                                  # 不花钱，先检查素材和配置

素材放在 bench/images/，搭配写在 bench/outfits.json（格式见 bench/outfits.example.json）。
跑过的图会跳过，中断后重跑会接着跑；加 --force 重跑全部。
结果在 bench/out/，打开 http://127.0.0.1:8000/bench 打分。
"""
import argparse, asyncio, json, sys, time

import httpx

import app as A
import providers as P

IMAGES, OUT = A.BENCH / "images", A.BENCH / "out"
NORM = OUT / "_norm"


def piece(x):
    """单品可以写成 "文件名" 或 {"file": "文件名", "name": "蓝白细条纹牛津纺"}。"""
    return (x, "") if isinstance(x, str) else (x["file"], x.get("name", ""))


def load_outfits():
    f = A.BENCH / "outfits.json"
    if not f.exists():
        sys.exit(f"没找到 {f}。复制 bench/outfits.example.json 改名为 outfits.json，按你的素材改。")
    spec = json.loads(f.read_text(encoding="utf-8"))
    NORM.mkdir(parents=True, exist_ok=True)
    items, outfits = {}, []

    def add(cat, x):
        name, label = piece(x)
        src = IMAGES / name
        if not src.exists():
            sys.exit(f"bench/images/ 里没有 {name}")
        iid = f"{cat}-{src.stem}"
        if iid not in items:
            dst = NORM / f"{iid}.jpg"
            if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
                A.normalize_image(src.read_bytes()).save(dst, quality=92)
            items[iid] = {"id": iid, "category": cat, "name": label, "file": dst.name}
        elif label:
            items[iid]["name"] = label
        return iid

    for o in spec["outfits"]:
        req = A.TryOn(person_id=add("person", o.get("person") or spec["person"]),
                      shirt_id=o.get("shirt") and add("shirt", o["shirt"]),
                      pants_id=o.get("pants") and add("pants", o["pants"]),
                      shoes_id=o.get("shoes") and add("shoes", o["shoes"]),
                      accessory_ids=[add("accessory", a) for a in o.get("accessories", [])],
                      tuck=o.get("tuck", "in"), sleeves=o.get("sleeves", "down"), collar=o.get("collar", "open"))
        outfits.append({"id": str(o["id"]), "note": o.get("note", ""), "req": req})
    return items, outfits


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pipelines", default=",".join(p for p in A.PIPELINES if P.ready(p)) or "bailian")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--only", default="", help="只跑这些搭配，逗号分隔的 id")
    ap.add_argument("--refine", action="store_true", help="试穿后加一步精修（百炼 aitryon-refiner）")
    ap.add_argument("--mock", action="store_true", help="不调模型，只检查流程")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--parallel", type=int, default=3)
    a = ap.parse_args()

    items, outfits = load_outfits()
    if a.only:
        keep = set(a.only.split(","))
        outfits = [o for o in outfits if o["id"] in keep]
    pipes = [p for p in a.pipelines.split(",") if p]
    for p in pipes:
        if p not in A.PIPELINES:
            sys.exit(f"不认识的流程 {p}，可选：{', '.join(A.PIPELINES)}")
        if not a.mock and not P.ready(p):
            sys.exit(f"{P.NAMES[p]} 的 key 还没填进 .env，先填 key 或去掉这条流程。")

    res_file = OUT / "results.json"
    old = json.loads(res_file.read_text(encoding="utf-8")) if res_file.exists() else {"runs": []}
    done = {(r["outfit"], r["pipeline"], r["run"], r.get("refine", False)) for r in old["runs"] if not r.get("error")}
    runs = [r for r in old["runs"] if not r.get("error")] if not a.force else []
    sem, lock = asyncio.Semaphore(a.parallel), asyncio.Lock()
    tag = "-refine" if a.refine else ""

    async def one(client, o, pipe, n):
        if not a.force and (o["id"], pipe, n, a.refine) in done:
            return
        req = o["req"].model_copy(update={"refine": a.refine})
        plan = A.build_plan(req, items, pipeline=pipe)
        rec = {"outfit": o["id"], "note": o["note"], "pipeline": pipe, "run": n, "refine": a.refine,
               "steps": [s["label"] for s in plan["steps"]], "cost": plan["cost"],
               "inputs": {k: items[v]["file"] for k, v in (("person", req.person_id), ("shirt", req.shirt_id),
                                                           ("pants", req.pants_id), ("shoes", req.shoes_id)) if v}
               | {f"accessory{i + 1}": items[x]["file"] for i, x in enumerate(req.accessory_ids)},
               "wear": {"tuck": req.tuck, "sleeves": req.sleeves, "collar": req.collar}}
        async with sem:
            t = time.time()
            try:
                im = await A.execute(plan, client, mock=a.mock, item_dir=NORM)
                d = OUT / f"{pipe}{tag}"
                d.mkdir(parents=True, exist_ok=True)
                name = f"{o['id']}_r{n}.jpg"
                im.save(d / name, quality=95)
                rec["file"] = f"out/{pipe}{tag}/{name}"
                print(f"  ✓ {pipe}{tag} 搭配 {o['id']} 第 {n} 次  {time.time() - t:.0f}s")
            except Exception as e:
                rec["error"] = str(e)
                print(f"  ✗ {pipe}{tag} 搭配 {o['id']} 第 {n} 次：{e}")
            rec["seconds"] = round(time.time() - t, 1)
        async with lock:
            runs.append(rec)
            res_file.write_text(json.dumps({"runs": runs}, ensure_ascii=False, indent=1), encoding="utf-8")

    total = len(outfits) * len(pipes) * a.runs
    est = sum(A.build_plan(o["req"].model_copy(update={"refine": a.refine}), items, pipeline=p)["cost"]
              for o in outfits for p in pipes) * a.runs
    print(f"{len(outfits)} 身搭配 × {len(pipes)} 条流程 × {a.runs} 次 = {total} 张，"
          f"{'演示模式不花钱' if a.mock else f'预计约 ¥{est:.1f}（已跑过的会跳过）'}")
    async with httpx.AsyncClient(timeout=180) as client:
        await asyncio.gather(*(one(client, o, p, n) for p in pipes for o in outfits for n in range(1, a.runs + 1)))
    bad = [r for r in runs if r.get("error")]
    print(f"\n完成。{len(runs) - len(bad)} 张成功，{len(bad)} 张失败。打开 http://127.0.0.1:8000/bench 打分。")


if __name__ == "__main__":
    asyncio.run(main())
