# 人格资产

这个目录**进仓库**，跟着项目走。运行时的记忆和日志在 `../data/`，不进仓库。

| 文件 | 作用 |
|---|---|
| `persona.md` | system prompt：它是谁、怎么说话、什么不做。改这个就是换人格 |
| `examples/good.md` | 被夸过的发言（学味道） |
| `examples/bad.md` | 被扣分的发言（学别这么接） |
| `examples/context-good.md` | 这个场面该开口 |
| `examples/context-bad.md` | 这个场面不该开口 |
| `knowledge/` | 知识库说明；实际内容写 `data/knowledge/` |
| `corpus/` | 可选的语感参考语料；不放受版权保护的内容 |

## 示例怎么用

首次启动时，`examples/*.md` 会被复制到 `data/samples/`。之后界面上的 ✔/✘ 只写
`data/samples/`，仓库里的示例保持干净。

想内置更多示例：改 `examples/`，删掉 `data/samples/` 里对应的文件，再启动。

## 格式

```text
- 【当时的场面】那句话
```

`【】` 里是判断时需要的语境；没有它，模型学到的就只是句子本身。每行一条，`#` 和 `>` 开头的行会被忽略。

## 别放什么

- 真实的观众昵称、uid、房间号
- 账号凭证、API key
- 从别处抄来的受版权保护语料

写人格的完整说明见 [`../docs/persona.md`](../docs/persona.md)。
