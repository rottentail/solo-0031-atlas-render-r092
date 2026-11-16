# 素材退役与历史隔离培训包

面向 Atlas Render 运行与集成人员的实操培训。全部材料基于**真实运行的服务进程**和**自制 PNG 样本**，
不修改任何源码，不使用概念示意图：每一个结论都有可复跑的命令、原始 HTTP 证据和像素级对照。

## 培训目标

完成本包后，学员应能独立解释并演示：

1. **接入**：自制 PNG 如何成为素材，入口校验拒绝什么，同 ID 内容为什么不能替换。
2. **冻结**：快照如何把场景修订与素材字节闭包一起固定，之后的修改为什么影响不到它。
3. **退役**：素材 `available → retired` 的真实效果，以及为什么退役后无法再冻结。
4. **再次冻结失败**：退役素材阻断新快照，但旧快照凭冻结字节继续渲染，结果逐像素不变。
5. **旧包跨实例渲染**：交换包在独立状态的第二实例上导入、冲突拒绝与像素级一致输出。
6. **退役与销毁字节的差别**：`retire` 是状态翻转且保留字节，`purge` 只销毁结果 PNG 字节且保留任务事实。

## 目录结构

```
training/
├── README.md                 本文件
├── assets/                   自制 PNG 源素材（make_png.py 生成，字节可重复）
│   ├── asset-a.png           16x16 RGBA，蓝底白斜带 + 透明象限
│   ├── asset-b.png           16x16 RGBA，橙底青反对角带 + 半透明象限（内容与 A 不同）
│   └── bad-not-png.bin       非 PNG 字节，演示入口 400 拒绝
├── lab/                      可复跑脚本（Python 标准库 + 一个只读摘要辅助 Go 程序）
│   ├── make_png.py           生成源素材
│   ├── lab_retire_flow.py    五段实验主驱动（启动两个真实实例，留存全部证据）
│   ├── make_contact_sheet.py 纯标准库 PNG 解码/编码，生成图像对照
│   ├── verify_evidence.py    离线核对证据（不启动服务）
│   ├── digesttool/main.go    调用项目自身摘要算法重封交换包（不改源码）
│   └── run_all.sh            一键复现
├── evidence/                 运行产物（重跑会重建）
│   ├── steps/                每一步的原始 JSON 响应与三份渲染 PNG
│   ├── contact-sheet.png     源素材 + 三渲染 + 像素差异对照图
│   ├── manifest.json         汇总清单
│   └── run/                  两个实例的状态文件与日志（可审计）
└── manual/
    ├── 00-固定内容与核对基准.md   所有摘要、ID、尺寸的固定值表
    ├── 01-接入与冻结.md
    ├── 02-退役与再冻结失败.md
    ├── 03-退役与销毁字节的差别.md
    ├── 04-旧包跨实例渲染.md
    └── 05-HTTP手动复现手册.md     不依赖 Python 驱动、用 curl 逐步复现
```

## 快速开始

```sh
# 需要 Go 1.22+（用于 go build，不修改源码）与 Python 3.9+
bash training/lab/run_all.sh
# 或指定 go 路径：
GO_BIN=/tmp/go-sdk/go/bin/go bash training/lab/run_all.sh
```

结束后用 `training/lab/verify_evidence.py` 的输出逐项核对，预期最后一行是
`VERIFY OK：全部核对通过`。

## 固定内容（详见 manual/00）

| 对象 | 固定值（每次复跑不变） |
|---|---|
| `asset-a.png` sha256 | `fce5d44a93777695889c03941c61d7517cce82a1dc766bb2734bd2b353fe3c2d` |
| `asset-b.png` sha256 | `798c47620e8c4f1d1407c9234be00a392b54ca804324e59b49a9e6e204eb8d26` |
| 冻结快照 ID / 摘要 | `snap-00000003` / `f4802cc460682c8041fe1ae0682d11b2f3ccf0a7ffd89292575294d6432086de` |
| 三份渲染 PNG sha256 | `3583e97c31e961ae2fdceba69ac941fac9d172efb8baea1b0c909e598ae23bcf`（三份完全相同） |
| 渲染尺寸 / 大小 | 320x240 / 6397 字节 |

## 边界声明

- 本培训包只通过 HTTP 与磁盘状态文件（只读审计）观察系统，不改动 `internal/`、`cmd/` 任何文件。
- `lab/digesttool` 是培训辅助程序，位于 `training/` 下，只调用项目导出的内部包做摘要计算。
- 证据中的时间戳（`createdAt`）每次运行会变；交换包外层摘要含时间戳，也会变；
  但素材摘要、快照内容摘要、渲染 PNG 摘要、实体 ID 均由固定输入决定，可重复。
