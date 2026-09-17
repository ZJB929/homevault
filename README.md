# HomeVault

一个面向家庭照片、证件扫描件和学习资料的内容寻址备份工具。它不会删除源文件；相同字节内容只保存一次；SQLite 记录每次快照的目录结构；所有对象都可以用 SHA-256 重新校验。

## 核心能力

- 流式复制和 SHA-256 计算，避免大文件被读取两次；
- 临时文件完整写入后再进入对象库；
- 相同内容跨目录、跨快照去重；
- 默认跳过符号链接，拒绝 Vault 位于源目录内部；
- 恢复时检查路径穿越，默认拒绝覆盖现有文件；
- `verify` 发现缺失或损坏对象时返回非零退出码；
- 附带最小权限 systemd service 和 timer。

## 快速开始

```bash
python3 homevault.py --vault /srv/homevault init
python3 homevault.py --vault /srv/homevault backup /data/family
python3 homevault.py --vault /srv/homevault list
python3 homevault.py --vault /srv/homevault verify
python3 homevault.py --vault /srv/homevault restore SNAPSHOT_ID /tmp/restore-check
```

运行测试：

```bash
python3 -m unittest -v test_homevault.py
```

当前测试覆盖相同内容去重、目录结构恢复、对象篡改检测和危险目录关系拒绝。

## 数据布局

```text
vault/
├── index.sqlite3
└── objects/
    └── sha256/
        └── ab/cdef...
```

## 重要限制

这是一个便于审计和学习的 MVP，不提供加密、远端副本、数据库应用一致性或勒索软件防护。不要把它作为唯一备份。请遵循 3-2-1 原则，并定期进行真实恢复演练。

完整设计、威胁模型和 openEuler 部署方法见 [技术文章](docs/article.md)。

## License

Apache-2.0
