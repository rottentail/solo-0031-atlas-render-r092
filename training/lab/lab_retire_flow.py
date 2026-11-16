#!/usr/bin/env python3
"""素材退役与历史隔离培训实验：对真实 Atlas Render 进程跑完整五段流程。

流程主线：
  1. 接入   自制 PNG 上传为素材，构造引用它的场景
  2. 冻结   快照把场景修订 + 素材字节闭包一起捕获
  3. 退役   素材 available -> retired，同 ID 不可替换为不同内容
  4. 再冻结 退役后新快照被 409 拒绝；旧快照渲染不读当前素材记录
  5. 跨实例 导出交换包，在第二个独立状态的实例上导入并渲染，逐像素比对

另含：退役 vs 销毁字节的差别（retire 保留字节 / purge 只清结果 PNG）。
只通过 HTTP 与服务交互，不修改任何源码。所有输出写入 evidence/ 供核对。
"""
import argparse
import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TRAINING = Path(__file__).resolve().parents[1]
ROOT = TRAINING.parent
ASSETS = TRAINING / "assets"


# ---------------------------------------------------------------- 进程与 HTTP
class Server:
    def __init__(self, binary: Path, workdir: Path, tag: str):
        self.tag = tag
        self.workdir = workdir
        workdir.mkdir(parents=True, exist_ok=True)
        self.log = open(workdir / f"server-{tag}.log", "ab")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.state_path = workdir / f"state-{tag}.json"
        env = dict(os.environ, ATLAS_ADDR=f"127.0.0.1:{port}",
                   ATLAS_STATE=str(self.state_path))
        self.proc = subprocess.Popen([str(binary)], env=env,
                                     stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                self.call("GET", "/healthz")
                return
            except (urllib.error.URLError, ConnectionError):
                if self.proc.poll() is not None:
                    raise RuntimeError(self.log.read_text())
                time.sleep(0.05)
        raise RuntimeError(f"server {tag} startup deadline exceeded")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=15)
        self.log.close()

    def call(self, method, path, body=None, status=200, headers=None, raw=False):
        hdr = dict(headers or {})
        data = body
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            hdr["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data,
                                     method=method, headers=hdr)
        try:
            resp = urllib.request.urlopen(req, timeout=20)
        except urllib.error.HTTPError as err:
            resp = err
        with resp:
            payload = resp.read()
            if resp.status != status:
                raise AssertionError(
                    f"{method} {path}: 期望 {status}，实际 {resp.status}: {payload[:400]!r}")
            if raw:
                return payload
            ctype = resp.headers.get("Content-Type", "")
            return json.loads(payload) if ctype.startswith("application/json") else payload

    def wait_job(self, job_id, want=("succeeded",)):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            job = self.call("GET", f"/v1/jobs/{job_id}")
            if job["state"] in want:
                return job
            if job["state"] in ("failed", "cancelled"):
                raise AssertionError(f"任务意外终态: {job}")
            time.sleep(0.05)
        raise AssertionError("等待任务超时")

    def submit(self, snapshot_id, key, scale=1):
        return self.call("POST", "/v1/jobs",
                         {"snapshotId": snapshot_id, "scale": scale}, 202,
                         {"Idempotency-Key": key})


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- 场景定义
def document() -> dict:
    """320x240 画布：浅米底 + 蓝渐变矩形 + 橙圆 + 引用 asset-a 的位图图层。"""
    return {
        "width": 320, "height": 240, "background": "#F5F1E8",
        "layers": [
            {"id": "plate", "kind": "rectangle", "width": 220, "height": 150,
             "transform": {"x": 45, "y": 35, "scaleX": 1, "scaleY": 1, "degrees": 5},
             "opacity": 1,
             "fill": {"kind": "linear", "color": "#145A70", "endColor": "#32A5A0",
                      "from": {"x": 0, "y": 0}, "to": {"x": 220, "y": 150}}},
            {"id": "accent", "kind": "ellipse", "width": 110, "height": 110,
             "transform": {"x": 175, "y": 95, "scaleX": 1, "scaleY": 1, "degrees": 0},
             "opacity": 0.85, "fill": {"kind": "solid", "color": "#E87E4F"}},
            {"id": "stamp", "kind": "image", "width": 96, "height": 96,
             "transform": {"x": 24, "y": 120, "scaleX": 1, "scaleY": 1, "degrees": 0},
             "opacity": 1, "assetId": "asset-a"},
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--binary", default="/tmp/go-sdk/go/bin/go",
                    help="go 可执行文件路径（用它构建 atlas）")
    ap.add_argument("--out", default=str(TRAINING / "evidence"))
    ap.add_argument("--keep", action="store_true", help="结束后保留服务进程")
    args = ap.parse_args()

    evidence = Path(args.out)
    steps = evidence / "steps"
    run_dir = evidence / "run"
    # 每次运行从空状态开始，保证步骤编号与证据可重复
    if run_dir.exists():
        shutil.rmtree(run_dir)
    if steps.exists():
        shutil.rmtree(steps)
    steps.mkdir(parents=True, exist_ok=True)
    binary = run_dir / "atlas"
    run_dir.mkdir(parents=True, exist_ok=True)

    # 构建摘要辅助工具（调用项目自身 model.CanonicalDigest，见 digesttool/main.go）
    digest_tool = run_dir / "digesttool"
    subprocess.run([args.binary, "build", "-o", str(digest_tool),
                    "./training/lab/digesttool"],
                   cwd=ROOT, check=True, timeout=120)

    def reseal(pkg: dict) -> dict:
        """用服务同款算法重算交换包外层摘要，返回新包。"""
        proc = subprocess.run([str(digest_tool)], input=json.dumps(pkg).encode(),
                              capture_output=True, check=True, timeout=30)
        return json.loads(proc.stdout)

    record = {}  # 汇总清单

    def save(step, name, payload):
        target = steps / f"{step}-{name}.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        record.setdefault(step, {})[name] = payload if not isinstance(payload, bytes) \
            else {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        return payload

    # ------------------------------------------------------------ 构建
    print("== 构建 atlas 二进制（不改源码，仅 go build） ==")
    subprocess.run([args.binary, "build", "-o", str(binary), "./cmd/atlas"],
                   cwd=ROOT, check=True, timeout=120)
    srv_a = Server(binary, run_dir / "instance-a", "a")
    srv_b = Server(binary, run_dir / "instance-b", "b")
    servers = [srv_a, srv_b]
    try:
        # ======================================================== 1. 接入
        print("\n== 第 1 段：接入（素材上传与入口校验） ==")
        png_a = (ASSETS / "asset-a.png").read_bytes()
        png_b = (ASSETS / "asset-b.png").read_bytes()
        bad = (ASSETS / "bad-not-png.bin").read_bytes()

        imported = srv_a.call("PUT", "/v1/assets/asset-a", png_a, 201)
        save("01-onboard", "put-asset-a-201", imported)
        assert imported["digest"] == hashlib.sha256(png_a).hexdigest()
        assert imported["state"] == "available"

        # 非 PNG 字节必须 400
        bad_resp = srv_a.call("PUT", "/v1/assets/broken", bad, 400)
        save("01-onboard", "put-bad-bytes-400", bad_resp)

        # 同 ID 同内容重复上传：幂等，返回同一记录
        again = srv_a.call("PUT", "/v1/assets/asset-a", png_a, 201)
        save("01-onboard", "put-same-bytes-idempotent-201", again)
        assert again["digest"] == imported["digest"]

        # 同 ID 不同内容：409，旧字节保持不变（摘要在汇总清单中复核）
        conflict = srv_a.call("PUT", "/v1/assets/asset-a", png_b, 409)
        save("01-onboard", "put-different-bytes-409", conflict)
        record["01-onboard"]["sourceAssetSha256"] = hashlib.sha256(png_a).hexdigest()

        scene = srv_a.call("PUT", "/v1/scenes/emblem", {
            "name": "退役训练-印记", "expectedRevision": 0, "document": document()})
        save("01-onboard", "put-scene-emblem-rev1", scene)
        assert scene["revision"] == 1

        # 引用一个不存在的素材必须 409
        doc_missing = document()
        doc_missing["layers"][2]["assetId"] = "asset-ghost"
        missing = srv_a.call("PUT", "/v1/scenes/emblem-missing", {
            "name": "悬空引用", "expectedRevision": 0, "document": doc_missing}, 409)
        save("01-onboard", "scene-references-missing-asset-409", missing)

        # ======================================================== 2. 冻结
        print("== 第 2 段：冻结（快照捕获文档修订与素材字节闭包） ==")
        snap1 = srv_a.call("POST", "/v1/scenes/emblem/snapshots",
                           {"expectedRevision": 1}, 201)
        save("02-freeze", "snapshot-snap1-201", {
            k: v for k, v in snap1.items() if k != "assets"})
        # 快照内素材字节与上传字节逐字节一致
        snap1_asset = snap1["assets"]["asset-a"]
        save("02-freeze", "snap1-closed-asset-meta", {
            "assetId": "asset-a", "digest": snap1_asset["digest"],
            "stateInsideSnapshot": snap1_asset["state"],
            "pngBytes": len(snap1_asset["png"]),
            "pngSha256": hashlib.sha256(
                base64.b64decode(snap1_asset["png"])).hexdigest(),
        })
        assert snap1_asset["digest"] == hashlib.sha256(png_a).hexdigest()

        # 冻结后再改场景（换背景），快照摘要不变 —— 历史隔离
        doc2 = document()
        doc2["background"] = "#0E2A38"
        scene2 = srv_a.call("PUT", "/v1/scenes/emblem", {
            "name": "退役训练-印记", "expectedRevision": 1, "document": doc2})
        save("02-freeze", "scene-revised-to-rev2", scene2)
        snap1_view = srv_a.call("GET", f"/v1/snapshots/{snap1['id']}")
        save("02-freeze", "snap1-digest-unchanged-after-revise", {
            "snapshotId": snap1["id"], "digest": snap1_view["digest"],
            "documentBackgroundInsideSnapshot":
                snap1_view["document"]["background"]})
        assert snap1_view["digest"] == snap1["digest"]
        assert snap1_view["document"]["background"] == "#F5F1E8"

        # 用冻结快照渲染第一份基准 PNG
        job1 = srv_a.submit(snap1["id"], "train-render-baseline")
        job1_done = srv_a.wait_job(job1["id"])
        png1 = srv_a.call("GET", f"/v1/artifacts/{job1_done['artifactId']}", raw=True)
        (steps / "02-baseline-render.png").write_bytes(png1)
        save("02-freeze", "baseline-render-job", {
            "jobId": job1_done["id"], "artifactId": job1_done["artifactId"],
            "state": job1_done["state"], "attempt": job1_done["attempt"],
            "pngBytes": len(png1), "pngSha256": hashlib.sha256(png1).hexdigest()})

        # ======================================================== 3. 退役
        print("== 第 3 段：退役（available -> retired，同 ID 不可替换内容） ==")
        retired = srv_a.call("POST", "/v1/assets/asset-a/retire", {}, 200)
        save("03-retire", "retire-response", retired)

        # 退役后同 ID 重新上传（即便内容相同也被拒）
        reup_same = srv_a.call("PUT", "/v1/assets/asset-a", png_a, 409)
        save("03-retire", "reupload-same-content-after-retire-409", reup_same)
        reup_diff = srv_a.call("PUT", "/v1/assets/asset-a", png_b, 409)
        save("03-retire", "reupload-different-content-after-retire-409", reup_diff)

        # 退役素材仍在状态中、字节与摘要可查；没有任何删除入口。
        # 审计磁盘状态文件（运维只读核对，不写入、不改服务）：
        state_a = json.loads(srv_a.state_path.read_text())
        retired_record = state_a["assets"]["asset-a"]
        save("03-retire", "on-disk-state-after-retire", {
            "assetId": retired_record["id"],
            "state": retired_record["state"],
            "digest": retired_record["digest"],
            "pngBytesStillPresent": len(base64.b64decode(retired_record["png"])),
            "pngSha256": hashlib.sha256(
                base64.b64decode(retired_record["png"])).hexdigest(),
            "note": "retire 只翻转 state，PNG 字节仍在状态文件中"})
        assert retired_record["state"] == "retired"
        assert hashlib.sha256(
            base64.b64decode(retired_record["png"])).hexdigest() \
            == hashlib.sha256(png_a).hexdigest()

        # （快照闭包里仍持有自己的副本，见第 4 段渲染成功）
        record["03-retire"] = {"rule": "retire 只翻转 state，不提供删除/替换 API"}

        # ======================================================== 4. 再冻结失败
        print("== 第 4 段：再冻结失败 + 旧快照渲染不受退役影响 ==")
        # 当前场景 rev2 仍引用 asset-a（已退役），新冻结必须 409
        refreeze = srv_a.call("POST", "/v1/scenes/emblem/snapshots",
                              {"expectedRevision": 2}, 409)
        save("04-refreeze-fail", "refreeze-after-retire-409", refreeze)

        # 直接保存引用退役素材的新修订也被 409（接入校验同样拦截）
        save_blocked = srv_a.call("PUT", "/v1/scenes/emblem", {
            "name": "退役训练-印记", "expectedRevision": 2,
            "document": document()}, 409)
        save("04-refreeze-fail", "save-revision-referencing-retired-409", save_blocked)

        # 旧快照仍 active：基于固定字节闭包再次渲染，结果与基准逐像素一致
        job2 = srv_a.submit(snap1["id"], "train-render-after-retire")
        job2_done = srv_a.wait_job(job2["id"])
        png2 = srv_a.call("GET", f"/v1/artifacts/{job2_done['artifactId']}", raw=True)
        (steps / "04-render-after-retire.png").write_bytes(png2)
        save("04-refreeze-fail", "render-from-old-snapshot-job", {
            "jobId": job2_done["id"], "artifactId": job2_done["artifactId"],
            "pngBytes": len(png2), "pngSha256": hashlib.sha256(png2).hexdigest()})
        compare12 = srv_a.call("POST", "/v1/compare", {
            "left": job1_done["artifactId"], "right": job2_done["artifactId"]})
        save("04-refreeze-fail", "compare-baseline-vs-after-retire", compare12)
        assert compare12["changedPixels"] == 0
        assert hashlib.sha256(png1).hexdigest() == hashlib.sha256(png2).hexdigest()

        # 撤回快照后新任务 409；同幂等键重放仍返回原任务（固定输入语义）
        revoke = srv_a.call("POST", f"/v1/snapshots/{snap1['id']}/revoke", {}, 200)
        save("04-refreeze-fail", "revoke-snap1", revoke)
        blocked = srv_a.call("POST", "/v1/jobs",
                             {"snapshotId": snap1["id"], "scale": 1}, 409,
                             {"Idempotency-Key": "train-render-blocked"})
        save("04-refreeze-fail", "new-job-on-revoked-409", blocked)
        replay = srv_a.call("POST", "/v1/jobs",
                            {"snapshotId": snap1["id"], "scale": 1}, 202,
                            {"Idempotency-Key": "train-render-baseline"})
        save("04-refreeze-fail", "idempotent-replay-returns-original-job", {
            "returnedJobId": replay["id"], "originalJobId": job1_done["id"]})
        assert replay["id"] == job1_done["id"]

        # ============================================ 4b. 退役 vs 销毁字节
        print("== 4b：退役（retire）与销毁字节（purge）的差别 ==")
        # purge 作用于“结果 PNG”，不作用于素材
        status_pre = srv_a.call("GET", "/v1/status")
        save("04b-purge", "status-before-purge", status_pre)
        purge = srv_a.call("DELETE",
                           f"/v1/artifacts/{job1_done['artifactId']}", {}, 200)
        save("04b-purge", "purge-artifact-response", purge)
        status_post = srv_a.call("GET", "/v1/status")
        save("04b-purge", "status-after-purge", status_post)
        save("04b-purge", "artifact-bytes-released", {
            "artifactBytesBefore": status_pre["artifactBytes"],
            "artifactBytesAfter": status_post["artifactBytes"],
            "released": status_pre["artifactBytes"] - status_post["artifactBytes"]})
        gone = srv_a.call("GET",
                          f"/v1/artifacts/{job1_done['artifactId']}", status=410)
        save("04b-purge", "get-purged-artifact-410", gone)
        job_after_purge = srv_a.call("GET", f"/v1/jobs/{job1_done['id']}")
        save("04b-purge", "job-still-succeeded-after-purge", {
            "jobId": job_after_purge["id"], "state": job_after_purge["state"],
            "artifactId": job_after_purge["artifactId"]})
        assert job_after_purge["state"] == "succeeded"
        # purge 之后素材记录及其字节完全不受影响
        state_after_purge = json.loads(srv_a.state_path.read_text())
        save("04b-purge", "asset-untouched-by-purge", {
            "assetId": state_after_purge["assets"]["asset-a"]["id"],
            "state": state_after_purge["assets"]["asset-a"]["state"],
            "pngBytesStillPresent": len(base64.b64decode(
                state_after_purge["assets"]["asset-a"]["png"]))})

        # ======================================================== 5. 跨实例
        print("== 第 5 段：旧包跨实例渲染（独立状态的第二实例） ==")
        # 第二实例状态盘与第一实例完全独立
        status_b_before = srv_b.call("GET", "/v1/status")
        save("05-cross-instance", "instance-b-status-before", status_b_before)
        assert status_b_before["sceneCount"] == 0
        assert status_b_before["assetCount"] == 0

        package = srv_a.call("GET", f"/v1/snapshots/{snap1['id']}/package")
        (steps / "05-exported-package.json").write_text(
            json.dumps(package, ensure_ascii=False, indent=2))
        save("05-cross-instance", "exported-package-meta", {
            "format": package["format"], "version": package["version"],
            "packageDigest": package["digest"],
            "snapshotId": package["snapshot"]["id"],
            "snapshotStateInsidePackage": package["snapshot"]["state"],
            "embeddedAssetDigests": {
                aid: a["digest"] for aid, a in
                package["snapshot"]["assets"].items()}})
        # 包内素材就是最初的源字节（来源可追溯）
        pkg_asset_png = base64.b64decode(
            package["snapshot"]["assets"]["asset-a"]["png"])
        save("05-cross-instance", "package-embedded-png-vs-source", {
            "sourceSha256": hashlib.sha256(png_a).hexdigest(),
            "packageEmbeddedSha256": hashlib.sha256(pkg_asset_png).hexdigest(),
            "byteIdentical": pkg_asset_png == png_a})
        assert pkg_asset_png == png_a

        # 5.1 干净实例导入成功，得到全新 active 场景（revision 重新从 1 开始）
        imported_scene = srv_b.call("POST", "/v1/imports/emblem-copy", {
            "name": "跨实例恢复的印记", "package": package}, 201)
        save("05-cross-instance", "import-on-clean-instance-201", {
            "id": imported_scene["id"], "revision": imported_scene["revision"],
            "state": imported_scene["state"],
            "documentEqual": imported_scene["document"] == snap1["document"]})
        assert imported_scene["revision"] == 1
        # 导入的素材在目标实例为 available（退役状态不随包传播为事实）
        asset_b_view = srv_b.call("GET", "/v1/status")
        save("05-cross-instance", "instance-b-status-after-import", asset_b_view)

        # 5.2 目标实例预置同 ID 异内容素材 -> 整笔导入失败，场景不落地
        srv_b.call("PUT", "/v1/assets/asset-b", png_b, 201)

        # 5.2a 外层摘要被破坏：先在内容校验之前被 400 拦下
        tampered_pkg = json.loads(json.dumps(package))
        tampered_pkg["digest"] = "0" * 64
        tampered = srv_b.call("POST", "/v1/imports/conflict-tampered", {
            "name": "篡改包", "package": tampered_pkg}, 400)
        save("05-cross-instance", "tampered-package-checksum-400", tampered)

        # 5.2b 构造合法摘要但素材同名异内容的包：
        # 把包内素材键改为 asset-b（字节仍是 asset-a），文档引用同步改写，
        # 再用服务同款 CanonicalDigest 重新封口 -> 越过摘要校验，触发 409
        conflict_pkg = json.loads(json.dumps(package))
        moved = conflict_pkg["snapshot"]["assets"]["asset-a"]
        moved["id"] = "asset-b"  # 记录内 ID 必须与闭包键名一致
        conflict_pkg["snapshot"]["assets"] = {"asset-b": moved}
        conflict_pkg["snapshot"]["document"]["layers"][2]["assetId"] = "asset-b"
        conflict_pkg = reseal(conflict_pkg)
        save("05-cross-instance", "repackaged-conflict-digest",
             {"digest": conflict_pkg["digest"]})
        conflict_resp = srv_b.call("POST", "/v1/imports/conflict-content", {
            "name": "同名异内容冲突", "package": conflict_pkg}, 409)
        save("05-cross-instance", "same-id-different-content-import-409",
             conflict_resp)
        srv_b.call("GET", "/v1/scenes/conflict-content", status=404)
        save("05-cross-instance", "conflict-scene-not-created-404",
             {"note": "整笔事务失败，目标场景不存在"})

        # 5.3 目标实例的 asset-b 退役后，同包再次导入 -> 同样整笔 409
        srv_b.call("POST", "/v1/assets/asset-b/retire", {}, 200)
        retire_conflict = srv_b.call("POST", "/v1/imports/conflict-retired", {
            "name": "退役素材冲突", "package": conflict_pkg}, 409)
        save("05-cross-instance", "retired-asset-import-409", retire_conflict)

        # 5.4 干净路径继续：导入场景冻结并渲染，与实例 A 基准逐像素对比
        snap_b = srv_b.call("POST", "/v1/scenes/emblem-copy/snapshots",
                            {"expectedRevision": 1}, 201)
        save("05-cross-instance", "freeze-on-instance-b", {
            "snapshotId": snap_b["id"], "digest": snap_b["digest"],
            "digestMatchesInstanceA": snap_b["digest"] == snap1["digest"]})
        assert snap_b["digest"] == snap1["digest"]
        job_b = srv_b.submit(snap_b["id"], "train-instance-b-render")
        job_b_done = srv_b.wait_job(job_b["id"])
        png_b_out = srv_b.call(
            "GET", f"/v1/artifacts/{job_b_done['artifactId']}", raw=True)
        (steps / "05-render-instance-b.png").write_bytes(png_b_out)
        save("05-cross-instance", "render-on-instance-b", {
            "jobId": job_b_done["id"], "artifactId": job_b_done["artifactId"],
            "pngBytes": len(png_b_out),
            "pngSha256": hashlib.sha256(png_b_out).hexdigest(),
            "identicalToInstanceABaseline":
                hashlib.sha256(png_b_out).hexdigest()
                == hashlib.sha256(png1).hexdigest()})
        assert png_b_out == png1

        # 事件流留档（两实例各自独立）
        save("05-cross-instance", "instance-a-events-tail",
             srv_a.call("GET", "/v1/events")[-12:])
        save("05-cross-instance", "instance-b-events-tail",
             srv_b.call("GET", "/v1/events")[-12:])
        save("05-cross-instance", "instance-a-final-status",
             srv_a.call("GET", "/v1/status"))
        save("05-cross-instance", "instance-b-final-status",
             srv_b.call("GET", "/v1/status"))

    finally:
        if not args.keep:
            for s in servers:
                s.stop()

    # ------------------------------------------------------------ 汇总清单
    record["meta"] = {
        "generatedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sourceAssets": {
            "asset-a.png": {"sha256": sha256_file(ASSETS / "asset-a.png"),
                            "bytes": (ASSETS / "asset-a.png").stat().st_size},
            "asset-b.png": {"sha256": sha256_file(ASSETS / "asset-b.png"),
                            "bytes": (ASSETS / "asset-b.png").stat().st_size},
        },
    }
    manifest_path = evidence / "manifest.json"
    manifest_path.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    print(f"\n证据清单: {manifest_path}")
    print(f"分步证据: {steps}/")
    print("全部断言通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
