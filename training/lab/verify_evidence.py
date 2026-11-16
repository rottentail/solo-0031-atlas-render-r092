#!/usr/bin/env python3
"""离线核对培训证据：只读取 training/ 下的文件，不启动任何服务。

核对项：
  1. 源素材 sha256 与接入证据、交换包内嵌字节、磁盘状态审计一致
  2. 三份渲染 PNG 字节级一致，并与 /v1/compare 的 changedPixels=0 互证
  3. 每一步的 HTTP 状态码与领域 code 符合手册断言
  4. 退役后磁盘状态仍含素材字节；purge 后结果字节释放、任务仍 succeeded
  5. 两实例快照摘要一致；导入冲突场景确实未落地
全部通过输出 VERIFY OK 并以 0 退出；任一不符打印差异并以 1 退出。
"""
import hashlib
import json
import sys
from pathlib import Path

TRAINING = Path(__file__).resolve().parents[1]
ASSETS = TRAINING / "assets"
STEPS = TRAINING / "evidence" / "steps"
failures = []


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def load(step, name):
    return json.loads((STEPS / f"{step}-{name}.json").read_text())


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    src_a = sha(ASSETS / "asset-a.png")
    src_b = sha(ASSETS / "asset-b.png")
    print(f"asset-a.png sha256 = {src_a}")
    print(f"asset-b.png sha256 = {src_b}")

    # 1. 接入证据
    put_a = load("01-onboard", "put-asset-a-201")
    check("接入：素材摘要等于源文件摘要", put_a["digest"] == src_a)
    check("接入：初始状态 available", put_a["state"] == "available")
    check("接入：非 PNG 字节被 400 拒绝",
          load("01-onboard", "put-bad-bytes-400")["code"] == "invalid")
    check("接入：同 ID 异内容被 409 拒绝",
          load("01-onboard", "put-different-bytes-409")["code"] == "conflict")
    check("接入：同字节重复上传幂等",
          load("01-onboard", "put-same-bytes-idempotent-201")["digest"] == src_a)
    check("接入：悬空引用场景被 409 拒绝",
          load("01-onboard",
               "scene-references-missing-asset-409")["code"] == "conflict")

    # 2. 冻结与历史隔离
    snap1 = load("02-freeze", "snapshot-snap1-201")
    closed = load("02-freeze", "snap1-closed-asset-meta")
    check("冻结：闭包素材摘要等于源摘要", closed["digest"] == src_a)
    check("冻结：闭包 PNG 字节等于源文件", closed["pngSha256"] == src_a)
    unchanged = load("02-freeze", "snap1-digest-unchanged-after-revise")
    check("冻结：场景修订后快照摘要不变", unchanged["digest"] == snap1["digest"])
    check("冻结：快照内文档仍是旧背景",
          unchanged["documentBackgroundInsideSnapshot"] == "#F5F1E8")

    # 3. 退役
    check("退役：接口返回 retired",
          load("03-retire", "retire-response")["state"] == "retired")
    check("退役：同字节重新上传 409",
          load("03-retire",
               "reupload-same-content-after-retire-409")["code"] == "conflict")
    check("退役：异字节重新上传 409",
          load("03-retire",
               "reupload-different-content-after-retire-409")["code"]
          == "conflict")
    disk = load("03-retire", "on-disk-state-after-retire")
    check("退役：磁盘状态仍保留全部 PNG 字节",
          disk["pngBytesStillPresent"] > 0 and disk["pngSha256"] == src_a)

    # 4. 再冻结失败 + 旧快照渲染
    check("再冻结：退役后新快照 409",
          load("04-refreeze-fail",
               "refreeze-after-retire-409")["code"] == "conflict")
    check("再冻结：保存引用退役素材的修订 409",
          load("04-refreeze-fail",
               "save-revision-referencing-retired-409")["code"] == "conflict")
    cmp12 = load("04-refreeze-fail", "compare-baseline-vs-after-retire")
    check("再冻结：像素比较 changedPixels=0", cmp12["changedPixels"] == 0)
    check("再冻结：撤回后新任务 409",
          load("04-refreeze-fail",
               "new-job-on-revoked-409")["code"] == "conflict")
    replay = load("04-refreeze-fail",
                  "idempotent-replay-returns-original-job")
    check("再冻结：同幂等键重放返回原任务",
          replay["returnedJobId"] == replay["originalJobId"])

    # 4b. retire vs purge
    check("purge：读取已清理结果返回 410",
          load("04b-purge", "get-purged-artifact-410")["code"] == "gone")
    check("purge：任务事实仍为 succeeded",
          load("04b-purge",
               "job-still-succeeded-after-purge")["state"] == "succeeded")
    released = load("04b-purge", "artifact-bytes-released")
    baseline_png_size = len((STEPS / "02-baseline-render.png").read_bytes())
    check("purge：结果字节计数按被清理 PNG 大小下降",
          released["released"] == baseline_png_size
          and released["artifactBytesAfter"]
          == released["artifactBytesBefore"] - baseline_png_size,
          json.dumps(released))
    untouched = load("04b-purge", "asset-untouched-by-purge")
    check("purge：素材记录与字节不受影响",
          untouched["state"] == "retired"
          and untouched["pngBytesStillPresent"] > 0)

    # 5. 跨实例
    pkg_meta = load("05-cross-instance", "exported-package-meta")
    check("跨实例：包内嵌素材摘要等于源摘要",
          pkg_meta["embeddedAssetDigests"]["asset-a"] == src_a)
    embed = load("05-cross-instance", "package-embedded-png-vs-source")
    check("跨实例：包内 PNG 与源文件逐字节相同", embed["byteIdentical"] is True)
    imp = load("05-cross-instance", "import-on-clean-instance-201")
    check("跨实例：导入得到 revision=1 的新 active 场景",
          imp["revision"] == 1 and imp["state"] == "active"
          and imp["documentEqual"] is True)
    check("跨实例：摘要篡改包 400",
          load("05-cross-instance",
               "tampered-package-checksum-400")["code"] == "invalid")
    check("跨实例：同名异内容导入 409",
          load("05-cross-instance",
               "same-id-different-content-import-409")["code"] == "conflict")
    check("跨实例：退役素材导入 409",
          load("05-cross-instance",
               "retired-asset-import-409")["code"] == "conflict")
    check("跨实例：冲突场景未落地（404 证据存在）",
          (STEPS / "05-cross-instance-conflict-scene-not-created-404.json").exists())
    snap_b = load("05-cross-instance", "freeze-on-instance-b")
    check("跨实例：两实例快照摘要一致",
          snap_b["digestMatchesInstanceA"] is True
          and snap_b["digest"] == snap1["digest"])

    # 6. 三份渲染字节级一致 + 对照图存在
    r1 = (STEPS / "02-baseline-render.png").read_bytes()
    r2 = (STEPS / "04-render-after-retire.png").read_bytes()
    r3 = (STEPS / "05-render-instance-b.png").read_bytes()
    check("渲染：三份 PNG 字节级一致", r1 == r2 == r3,
          f"{sha256_of(r1)} vs {sha256_of(r2)} vs {sha256_of(r3)}")
    render_meta = load("05-cross-instance", "render-on-instance-b")
    check("渲染：实例 B 输出摘要等于实例 A 基准",
          render_meta["identicalToInstanceABaseline"] is True)
    sheet = TRAINING / "evidence" / "contact-sheet.png"
    check("对照图：contact-sheet.png 已生成且为 PNG",
          sheet.exists() and sheet.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n")

    print("\n" + ("VERIFY OK：全部核对通过" if not failures
                  else f"VERIFY FAILED：{len(failures)} 项不符: {failures}"))
    return 0 if not failures else 1


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


if __name__ == "__main__":
    sys.exit(main())
