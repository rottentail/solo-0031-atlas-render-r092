# 05 HTTP 手动复现手册（curl 版）

本手册用两个真实 atlas 进程 + `curl` 逐步复现全部五段流程，不依赖 `lab/` 的 Python 驱动。
适合课堂演示与排障练习。所有命令可直接复制；预期状态码与关键字段已标注。

> 关于 ID：实体 ID（`snap-…`、`job-…`、`png-…`）由实例状态序列决定。
> 失败的写请求也会消耗事件序列，因此**手动练习时请用下方的动态提取方式取 ID，不要背序号**。
> 从空状态、按 `run_all.sh` 的固定步骤执行时，序号才稳定为手册 00 中的固定值。

## 0. 准备

```sh
W=/workspace   # 改成你的仓库根目录；全程使用绝对路径，避免工作目录漂移
cd "$W"
go build -o /tmp/atlas ./cmd/atlas          # 不改源码，只构建
rm -rf /tmp/lab && mkdir -p /tmp/lab/a /tmp/lab/b
python3 -I training/lab/make_png.py training/assets
sha256sum training/assets/asset-a.png
# 预期 fce5d44a93777695889c03941c61d7517cce82a1dc766bb2734bd2b353fe3c2d
```

启动两个实例（各自独立状态盘与端口）：

```sh
ATLAS_ADDR=127.0.0.1:18081 ATLAS_STATE=/tmp/lab/a/state.json /tmp/atlas >/tmp/lab/a.log 2>&1 &
ATLAS_ADDR=127.0.0.1:18082 ATLAS_STATE=/tmp/lab/b/state.json /tmp/atlas >/tmp/lab/b.log 2>&1 &
A=http://127.0.0.1:18081; B=http://127.0.0.1:18082
sleep 1
curl -s $A/healthz; curl -s $B/healthz     # 各返回 {"status":"ready"}
```

## 1. 接入

```sh
# 合法素材（PNG 字节直接作为请求体）→ 201
curl -s -w '\n[%{http_code}]\n' -XPUT --data-binary @$W/training/assets/asset-a.png \
  $A/v1/assets/asset-a
# 预期 digest=fce5d44a…3c2d，state=available

# 非 PNG 字节 → 400 invalid
curl -s -w '\n[%{http_code}]\n' -XPUT \
  --data-binary @$W/training/assets/bad-not-png.bin $A/v1/assets/broken

# 同 ID 不同内容 → 409 conflict
curl -s -w '\n[%{http_code}]\n' -XPUT \
  --data-binary @$W/training/assets/asset-b.png $A/v1/assets/asset-a

# 创建引用 asset-a 的场景（revision 1）
cat > /tmp/lab/scene.json <<'JSON'
{"name":"退役训练-印记","expectedRevision":0,"document":{
 "width":320,"height":240,"background":"#F5F1E8","layers":[
  {"id":"plate","kind":"rectangle","width":220,"height":150,
   "transform":{"x":45,"y":35,"scaleX":1,"scaleY":1,"degrees":5},"opacity":1,
   "fill":{"kind":"linear","color":"#145A70","endColor":"#32A5A0",
           "from":{"x":0,"y":0},"to":{"x":220,"y":150}}},
  {"id":"accent","kind":"ellipse","width":110,"height":110,
   "transform":{"x":175,"y":95,"scaleX":1,"scaleY":1,"degrees":0},
   "opacity":0.85,"fill":{"kind":"solid","color":"#E87E4F"}},
  {"id":"stamp","kind":"image","width":96,"height":96,
   "transform":{"x":24,"y":120,"scaleX":1,"scaleY":1,"degrees":0},
   "opacity":1,"assetId":"asset-a"}]}}
JSON
curl -s -XPUT -H 'Content-Type: application/json' --data @/tmp/lab/scene.json \
  $A/v1/scenes/emblem        # 预期 revision=1, state=active
```

## 2. 冻结与基准渲染

```sh
# 冻结 revision 1，动态提取快照 ID
SNAP=$(curl -s -XPOST -H 'Content-Type: application/json' \
  -d '{"expectedRevision":1}' $A/v1/scenes/emblem/snapshots \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['id'])")
echo "$SNAP"                 # 干净起步下为 snap-00000003，摘要 f4802cc4…86de

# 提交渲染并等待 succeeded
JOB=$(curl -s -XPOST -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: train-render-baseline' \
  -d "{\"snapshotId\":\"$SNAP\",\"scale\":1}" $A/v1/jobs \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
sleep 1
ART=$(curl -s $A/v1/jobs/$JOB \
  | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['state']=='succeeded'; print(d['artifactId'])")
curl -s -o /tmp/lab/baseline.png $A/v1/artifacts/$ART
sha256sum /tmp/lab/baseline.png      # 预期 3583e97c…23bcf
```

## 3. 退役

```sh
curl -s -XPOST $A/v1/assets/asset-a/retire            # {"state":"retired"}
# 同字节重新上传也被 409（退役是终态）
curl -s -w '\n[%{http_code}]\n' -XPUT \
  --data-binary @$W/training/assets/asset-a.png $A/v1/assets/asset-a
# 磁盘只读审计：字节仍在
python3 -I -c "
import json,base64,hashlib
s=json.load(open('/tmp/lab/a/state.json'))['assets']['asset-a']
print(s['state'], len(base64.b64decode(s['png'])),
      hashlib.sha256(base64.b64decode(s['png'])).hexdigest())"
# 预期 retired 126 fce5d44a…3c2d
```

## 4. 再冻结失败，旧快照渲染不变

```sh
# 4.1 退役后，连“保存仍引用该素材的修订”都会 409（事务内引用检查）
python3 -I -c "
import json
d=json.load(open('/tmp/lab/scene.json'))
d['expectedRevision']=1; d['document']['background']='#0E2A38'
json.dump(d, open('/tmp/lab/scene2.json','w'))"
curl -s -w '\n[%{http_code}]\n' -XPUT -H 'Content-Type: application/json' \
  --data @/tmp/lab/scene2.json $A/v1/scenes/emblem
# 409 scene references unavailable asset

# 4.2 新冻结同样 409（当前 revision 仍是 1；上一步修订未落地）
curl -s -w '\n[%{http_code}]\n' -XPOST -H 'Content-Type: application/json' \
  -d '{"expectedRevision":1}' $A/v1/scenes/emblem/snapshots
# 409 snapshot references unavailable asset

# 4.3 旧快照凭冻结字节继续渲染，输出与基准逐像素相同
JOB2=$(curl -s -XPOST -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: train-render-after-retire' \
  -d "{\"snapshotId\":\"$SNAP\",\"scale\":1}" $A/v1/jobs \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
sleep 1
ART2=$(curl -s $A/v1/jobs/$JOB2 | python3 -c "import json,sys; print(json.load(sys.stdin)['artifactId'])")
curl -s -o /tmp/lab/after.png $A/v1/artifacts/$ART2
cmp /tmp/lab/baseline.png /tmp/lab/after.png && echo PIXEL-IDENTICAL

# 服务端解码像素比较
curl -s -XPOST -H 'Content-Type: application/json' \
  -d "{\"left\":\"$ART\",\"right\":\"$ART2\"}" $A/v1/compare
# 预期 {"changedPixels":0,"maxDelta":0,"meanDelta":0}

# 4.4 撤回与幂等重放
curl -s -XPOST $A/v1/snapshots/$SNAP/revoke          # {"state":"revoked"}
curl -s -w '\n[%{http_code}]\n' -XPOST -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: brand-new-key' \
  -d "{\"snapshotId\":\"$SNAP\",\"scale\":1}" $A/v1/jobs      # 409 snapshot is revoked
curl -s -XPOST -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: train-render-baseline' \
  -d "{\"snapshotId\":\"$SNAP\",\"scale\":1}" $A/v1/jobs \
  | python3 -c "import json,sys; print('replay ->', json.load(sys.stdin)['id'])"
# 重放返回原任务 ID（等于 $JOB）
```

## 4b. 清理结果字节（purge）

```sh
curl -s $A/v1/status | python3 -m json.tool        # 清理前 artifactBytes=12794
curl -s -XDELETE $A/v1/artifacts/$ART              # {"state":"purged"}
curl -s -w '\n[%{http_code}]\n' $A/v1/artifacts/$ART                 # 410 gone
curl -s $A/v1/jobs/$JOB | grep -o '"state":"succeeded"'             # 任务事实不变
curl -s $A/v1/status | python3 -m json.tool        # artifactBytes 降为 6397
```

## 5. 跨实例

```sh
# 实例 B 导入前为空
curl -s $B/v1/status     # sceneCount/assetCount/snapshotCount 全为 0

# 5.1 从实例 A 导出包并干净导入
curl -s $A/v1/snapshots/$SNAP/package > /tmp/lab/pkg.json
python3 -I -c "
import json
pkg=json.load(open('/tmp/lab/pkg.json'))
json.dump({'name':'跨实例恢复的印记','package':pkg}, open('/tmp/lab/import.json','w'))"
curl -s -w '\n[%{http_code}]\n' -XPOST -H 'Content-Type: application/json' \
  --data @/tmp/lab/import.json $B/v1/imports/emblem-copy     # 201, revision=1, active

# 5.2 实例 B 冻结（摘要与实例 A 相同）并渲染（输出与基准字节相同）
SNAPB=$(curl -s -XPOST -H 'Content-Type: application/json' \
  -d '{"expectedRevision":1}' $B/v1/scenes/emblem-copy/snapshots \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
JOBB=$(curl -s -XPOST -H 'Content-Type: application/json' -H 'Idempotency-Key: b-render' \
  -d "{\"snapshotId\":\"$SNAPB\",\"scale\":1}" $B/v1/jobs \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
sleep 1
ARTB=$(curl -s $B/v1/jobs/$JOBB | python3 -c "import json,sys; print(json.load(sys.stdin)['artifactId'])")
curl -s -o /tmp/lab/instance-b.png $B/v1/artifacts/$ARTB
cmp /tmp/lab/baseline.png /tmp/lab/instance-b.png && echo CROSS-INSTANCE-IDENTICAL

# 5.3 失败路径 A：外层摘要破坏 → 400
python3 -I -c "
import json
p=json.load(open('/tmp/lab/pkg.json')); p['digest']='0'*64
json.dump({'name':'篡改包','package':p}, open('/tmp/lab/bad1.json','w'))"
curl -s -w '\n[%{http_code}]\n' -XPOST -H 'Content-Type: application/json' \
  --data @/tmp/lab/bad1.json $B/v1/imports/x1                 # 400 package checksum mismatch

# 5.4 失败路径 B：同名异内容 → 409（先用项目同款算法合法重封口）
go build -o /tmp/lab/digesttool ./training/lab/digesttool
curl -s -o /dev/null -XPUT --data-binary @$W/training/assets/asset-b.png \
  $B/v1/assets/asset-b
python3 -I -c "
import json,subprocess
p=json.load(open('/tmp/lab/pkg.json'))
moved=p['snapshot']['assets']['asset-a']; moved['id']='asset-b'
p['snapshot']['assets']={'asset-b':moved}
p['snapshot']['document']['layers'][2]['assetId']='asset-b'
sealed=subprocess.run(['/tmp/lab/digesttool'],
                      input=json.dumps(p).encode(), capture_output=True).stdout
json.dump({'name':'同名异内容冲突','package':json.loads(sealed)},
          open('/tmp/lab/import-conflict.json','w'))"
curl -s -w '\n[%{http_code}]\n' -XPOST -H 'Content-Type: application/json' \
  --data @/tmp/lab/import-conflict.json $B/v1/imports/x2      # 409 destination asset conflicts
curl -s -w '\n[%{http_code}]\n' $B/v1/scenes/x2               # 404 整笔回滚未落地

# 5.5 失败路径 C：同名素材在目标实例退役 → 同样 409
curl -s -XPOST $B/v1/assets/asset-b/retire >/dev/null
curl -s -w '\n[%{http_code}]\n' -XPOST -H 'Content-Type: application/json' \
  --data @/tmp/lab/import-conflict.json $B/v1/imports/x3      # 409
```

## 收尾

```sh
pkill -f /tmp/atlas
# 全自动化等价物：bash training/lab/run_all.sh
# 离线核对：python3 -I training/lab/verify_evidence.py
```

## 练习中实测到的两个易错点

1. **相对路径陷阱**：在非仓库根目录执行时，`@training/assets/...` 会解析失败并发出
   Content-Length: 0 的请求，服务返回 `PNG size must be 1..2MiB`。始终用绝对路径或先 `cd "$W"`。
2. **序号漂移**：任何写请求（含失败的）都会推进事件序列，手动练习的 `job-…/png-…` 编号
   可能与手册 00 的干净运行不同；摘要和字节 sha256 不受影响，核对以它们为准。
