// 培训辅助工具：用项目自身的摘要算法重算交换包的两层摘要。
//   - snapshot.digest  = model.SnapshotDigest(snapshot)，覆盖文档与素材闭包
//   - package.digest   = model.CanonicalDigest(snapshot)，外层封口摘要
// 目的：让学员能在“已篡改快照内容”的包上恢复出合法摘要，
// 从而把验证推进到素材闭包冲突（409）这一层，而不是停在摘要不符（400）。
// 只读 stdin 的 JSON，写 stdout 的 JSON，不接触服务状态，也不改任何源码。
package main

import (
	"atlas-render/internal/bundle"
	"atlas-render/internal/model"
	"encoding/json"
	"io"
	"os"
)

func main() {
	raw, e := io.ReadAll(os.Stdin)
	if e != nil {
		panic(e)
	}
	var p bundle.Package
	if e := json.Unmarshal(raw, &p); e != nil {
		panic(e)
	}
	// 与 service.Freeze / bundle.Export 完全相同的摘要算法
	p.Snapshot.Digest = model.SnapshotDigest(p.Snapshot)
	p.Digest = model.CanonicalDigest(p.Snapshot)
	out, e := json.Marshal(p)
	if e != nil {
		panic(e)
	}
	os.Stdout.Write(out)
}
