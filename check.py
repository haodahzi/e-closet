"""一条命令诊断即梦（火山方舟）连不连得上：网络、key、模型开通。

    python check.py            # 免费：测网络、key、自动检查用的视觉模型（几乎不花钱）
    python check.py --image    # 再真的生成一张小图，确认 Seedream 能用（约 ¥0.2）
"""
import os, sys, time
from pathlib import Path

import httpx

ROOT = Path(__file__).parent
env = ROOT / ".env"
if env.exists():
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.split(" #")[0].strip().strip('"').strip("'"))

import providers as P  # noqa: E402

ok = lambda m: print("  ✓ " + m)
bad = lambda m: print("  ✗ " + m)
tip = lambda m: print("    → " + m)

print("\n即梦（火山方舟）连接检查\n")
key = P.env("ARK_API_KEY")
if not key:
    bad(".env 里没有 ARK_API_KEY")
    tip("复制 .env.example 为 .env，填上 ARK_API_KEY=你的key")
    sys.exit(1)
ok(f"读到 ARK_API_KEY（{key[:4]}…{key[-4:]}，共 {len(key)} 位）")

proxies = {k: v for k, v in os.environ.items() if k.lower() in ("http_proxy", "https_proxy", "all_proxy")}
if proxies:
    print(f"  · 终端里设置了代理：{', '.join(f'{k}={v}' for k, v in proxies.items())}")
    tip("火山方舟是国内服务，走海外代理可能很慢或连不上；不行就关掉代理再试")

H = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

# 1. 网络
t = time.time()
try:
    r = httpx.get(P.ARK.rsplit("/api/", 1)[0], timeout=P.timeout(20))
    ok(f"能连上 {P.ARK.split('/')[2]}（{time.time() - t:.1f} 秒）")
except Exception as e:
    bad(f"连不上 {P.ARK.split('/')[2]}：{type(e).__name__}: {e}")
    tip("检查网络；开着 VPN/代理的话先关掉再试")
    sys.exit(1)

# 2. key + 视觉模型（自动检查用，一次几乎不花钱）
model = P.env("CHECK_MODEL", "doubao-seed-1-6-250615")
t = time.time()
try:
    r = httpx.post(f"{P.ARK}/chat/completions", headers=H, timeout=P.timeout(60),
                   json={"model": model, "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]})
    if r.status_code == 200:
        ok(f"key 有效，自动检查用的模型 {model} 可以用（{time.time() - t:.1f} 秒）")
    else:
        msg = r.text[:300]
        if r.status_code == 401:
            bad("key 被拒绝（401）：key 不对或复制不完整");
            tip("到火山方舟「API Key 管理」重新复制，整串填进 .env")
            sys.exit(1)
        bad(f"自动检查用的模型 {model} 不能用（{r.status_code}）：{msg}")
        tip("不影响出图，只是不做自动检查。到「开通管理」开通 Doubao-Seed-1.6；"
            "或者建推理接入点，把 ep- 开头的 ID 填到 .env 的 CHECK_MODEL")
except Exception as e:
    bad(f"请求超时或中断：{type(e).__name__}: {e}")

# 3. Seedream 真的出一张图（要钱，需加 --image）
if "--image" in sys.argv:
    model = P.env("SEEDREAM_MODEL", "doubao-seedream-4-5-251128")
    print(f"\n  · 用 {model} 生成一张测试图，通常 10–40 秒…")
    t = time.time()
    try:
        r = httpx.post(f"{P.ARK}/images/generations", headers=H, timeout=P.timeout(300),
                       json={"model": model, "prompt": "a white cotton shirt on a wooden hanger, plain background",
                             "size": "2048x2048", "response_format": "url", "watermark": False,
                             "sequential_image_generation": "disabled"})
        j = r.json() if "json" in r.headers.get("content-type", "") else {}
        if r.status_code == 200 and j.get("data"):
            ok(f"Seedream 能出图（{time.time() - t:.0f} 秒）：{j['data'][0].get('url', '')[:80]}…")
        else:
            bad(f"Seedream 出图失败（{r.status_code}）：{r.text[:400]}")
            tip("「模型不存在 / not found」：到「开通管理」开通 Doubao-Seedream-4.5，"
                "或建推理接入点，把 ep- 开头的 ID 填到 .env 的 SEEDREAM_MODEL")
            tip("「余额不足 / quota」：到火山引擎费用中心充值")
    except Exception as e:
        bad(f"出图请求超时或中断（{time.time() - t:.0f} 秒）：{type(e).__name__}: {e}")
else:
    print("\n  · 想确认 Seedream 真能出图：python check.py --image（约 ¥0.2）")
print()
