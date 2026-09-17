# 在 openEuler 上做一个真正可恢复的家庭备份：HomeVault 的内容寻址、去重与校验设计

> 项目：HomeVault  
> 技术栈：openEuler、Python 3、SQLite、SHA-256、systemd  
> 适用场景：家庭照片、扫描件、学习资料的本地快照备份  
> 开源协议建议：Apache-2.0

## 1. 备份的终点不是“复制成功”

家庭文件通常分散在手机导出目录、电脑桌面、聊天软件和移动硬盘里。最常见的做法是隔一段时间把整个目录复制一遍，结果往往是：

- 同一张照片在多个目录重复占空间；
- 不知道某次复制是否完整；
- 文件损坏后，备份中的损坏副本仍会被当成正常文件；
- 真要恢复时，才发现没有记录原始目录结构；
- 定时任务以 root 运行，一段脚本拥有了不必要的系统权限。

HomeVault 的目标不是替代成熟备份软件，而是用一个约 200 行、容易审计的开源 MVP 把备份的核心链路讲透：

```text
扫描源目录
   ↓
流式计算 SHA-256，同时写临时对象
   ↓
按摘要保存唯一对象 objects/sha256/ab/cdef...
   ↓
SQLite 记录 快照 + 相对路径 + 摘要 + mtime
   ↓
verify 重新读取对象并校验
   ↓
restore 在空目录进行恢复演练
```

源码位于配套目录 `homevault/`。本文中的仓库链接占位符应在发布前替换为你的 GitCode 地址和固定 tag。

## 2. 内容寻址为什么能去重

普通备份以路径为中心：`相册/毕业照.jpg` 和 `微信导出/毕业照.jpg` 被视为两个对象。HomeVault 先读取内容并计算 SHA-256：

```text
digest = SHA256(file_bytes)
object_path = objects/sha256/{digest[0:2]}/{digest[2:]}
```

只要两个文件的字节完全相同，它们就指向同一个对象。路径信息单独存入 SQLite：

```sql
CREATE TABLE objects (
    digest TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE files (
    snapshot_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    digest TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, relative_path)
);
```

这种设计把“内容”和“文件名”解耦：同一内容可以出现在多个快照、多个目录中，但磁盘只保存一份对象。

SHA-256 在这里承担的是内容标识与意外损坏检测，不是数字签名。攻击者如果能够同时修改对象和数据库，单纯的哈希无法证明数据未被恶意篡改；这需要密钥认证、只读介质或远端不可变存储。

## 3. 为什么边读边哈希，而不是先算再复制

最直观的实现是先读文件算摘要，再读一次复制。对大视频文件，这会产生双倍 I/O。HomeVault 在一次流式读取中同时完成哈希和临时写入：

```python
hasher = hashlib.sha256()
with os.fdopen(fd, "wb") as target, source.open("rb") as src:
    while chunk := src.read(1024 * 1024):
        hasher.update(chunk)
        target.write(chunk)
```

写完后执行三个动作：

1. `flush` 把 Python 缓冲区推给操作系统；
2. `fsync` 请求操作系统把文件内容同步到稳定存储；
3. `os.replace` 把同一文件系统内的临时对象重命名到最终摘要路径。

```python
target.flush()
os.fsync(target.fileno())
os.replace(temp_path, final_path)
```

为什么不能一开始就写最终路径？因为进程中断后会留下一个名字“正确”、内容却不完整的对象。临时文件只有完整写入并算出摘要后才获得最终名字。

仍要承认一个边界：`fsync` 了文件不等于整个目录项在所有存储设备上都绝对持久。严苛场景还应同步父目录，并针对文件系统和硬件做掉电测试。家庭 MVP 把这个行为写清楚，比笼统声称“原子写入”更负责。

## 4. 备份源在读取时变化怎么办

当前版本在读取前后没有再次比较文件大小和修改时间，因此源文件在备份过程中被修改时，保存的是“读取期间观察到的字节流”。摘要与对象仍然一致，但它不一定对应修改前或修改后的完整业务状态。

照片和扫描件通常是写完后不再修改的文件，这个风险可以接受；数据库、虚拟机镜像和正在录制的视频则不适合直接扫描。

改进方案有三类：

- 读取前后比较 `size`、`mtime_ns`，变化时重试或跳过；
- 在 Btrfs/LVM 快照上执行备份；
- 让应用先导出一致性快照，再交给 HomeVault。

备份工具必须说明一致性模型，否则“每个文件都成功读取”不代表“整个应用状态可恢复”。

## 5. 快照元数据为什么用 SQLite

一个 JSON 文件很容易上手，但随着快照增加，查询“某个摘要被哪些路径引用”“一个快照有多少文件”会变得低效。SQLite 提供事务、索引和约束，同时仍是一个便携文件。

HomeVault 在写入快照时使用一笔数据库事务。若元数据插入失败，事务回滚；已经写入的内容对象可能暂时成为未引用对象，但不会出现半个快照被当成完整快照。

未引用对象属于可回收空间，不过垃圾回收不能贸然实现。正确流程应该是：

```text
读取数据库中所有被引用摘要
→ 与 objects 目录求差集
→ 先生成报告
→ 设置保留期
→ 再由显式 gc 命令删除
```

当前版本故意没有自动删除：备份系统中的误删风险，通常比多占一点空间更严重。

## 6. 路径穿越与覆盖保护

恢复功能从数据库读取相对路径。如果数据库被损坏或篡改，恶意路径 `../../etc/passwd` 可能把文件写出目标目录。因此恢复前必须检查：

```python
pure = PurePosixPath(relative_path)
if pure.is_absolute() or ".." in pure.parts:
    raise RuntimeError(f"unsafe path in index: {relative_path}")
```

默认恢复也不会覆盖现有文件：

```python
if target.exists() and not overwrite:
    raise FileExistsError(f"refusing to overwrite: {target}")
```

这两个默认值会让恢复稍显麻烦，却能防止“选错目标目录”演变成新的数据事故。需要覆盖时必须显式传入 `--overwrite`。

符号链接同样默认跳过。跟随符号链接可能离开源目录，也可能形成循环。未来如需支持，应把链接本身作为一种元数据类型保存，而不是默默跟随。

## 7. 在 openEuler 上运行

准备一个普通服务账号，并让它只读源目录、可写备份目录。以下命令中的路径要替换为真实环境：

```bash
sudo useradd --system --home-dir /var/lib/homevault --shell /sbin/nologin homevault
sudo install -d -o homevault -g homevault /srv/homevault
sudo install -d -m 0750 /etc/homevault
sudo install -m 0755 homevault.py /opt/homevault/homevault.py
```

配置 `/etc/homevault/homevault.conf`：

```text
SOURCE_DIR=/data/family
VAULT_DIR=/srv/homevault
```

先手工初始化和备份：

```bash
sudo -u homevault python3 /opt/homevault/homevault.py \
  --vault /srv/homevault init

sudo -u homevault python3 /opt/homevault/homevault.py \
  --vault /srv/homevault backup /data/family
```

再执行完整校验：

```bash
sudo -u homevault python3 /opt/homevault/homevault.py \
  --vault /srv/homevault verify
```

`verify` 发现缺失或损坏对象时退出码为 2，便于 systemd 或监控系统识别异常。

## 8. systemd 不只是“定时启动”

配套 service 使用了一组基础隔离选项：

```ini
[Service]
Type=oneshot
User=homevault
Group=homevault
EnvironmentFile=/etc/homevault/homevault.conf
ExecStart=/usr/bin/python3 /opt/homevault/homevault.py --vault ${VAULT_DIR} backup ${SOURCE_DIR}
PrivateTmp=true
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadOnlyPaths=${SOURCE_DIR}
ReadWritePaths=${VAULT_DIR}
```

核心原则是：

- 不使用 root 运行备份代码；
- 源目录只读，备份工具没有修改原文件的权限；
- 系统目录只读；
- 只有 Vault 目录可写；
- `NoNewPrivileges` 阻止进程通过 setuid 等方式获得新权限。

定时器采用 `Persistent=true`。如果机器在计划时间关机，重新开机后仍会补跑；`RandomizedDelaySec` 则避免许多设备在同一秒集中访问磁盘。

```ini
[Timer]
OnCalendar=*-*-* 03:15:00
Persistent=true
RandomizedDelaySec=20m
```

启用前先检查配置：

```bash
sudo systemd-analyze verify /etc/systemd/system/homevault.service
sudo systemctl daemon-reload
sudo systemctl enable --now homevault.timer
systemctl list-timers homevault.timer
```

## 9. 恢复演练才是备份验收

建议每月自动校验，每季度进行一次恢复演练：

```bash
python3 homevault.py --vault /srv/homevault list
python3 homevault.py --vault /srv/homevault restore \
  20260917T031500000000Z /tmp/homevault-restore-check
```

恢复完成后，随机抽取照片、PDF、压缩包打开，并比较文件数量。只看 `verify` 不够，因为 `verify` 只证明对象摘要一致，不证明快照元数据包含了你以为已经备份的所有目录。

更完整的验收指标包括：

| 指标 | 含义 |
|---|---|
| 源文件数 vs 快照文件数 | 是否漏扫 |
| 唯一对象数 / 文件数 | 去重效果 |
| 逻辑字节 / 对象实际字节 | 空间节省比例 |
| verify 缺失数、损坏数 | 存储完整性 |
| 恢复耗时 | 故障时是否可接受 |
| 随机抽样可打开率 | 文件格式层面的可用性 |

## 10. 本项目的实测与未实测部分

我为核心链路编写了四个单元测试：相同内容去重、目录结构恢复、篡改检测、禁止 Vault 位于源目录内部。

```text
test_deduplicates_identical_files ... ok
test_refuses_vault_inside_source ... ok
test_restore_preserves_content ... ok
test_verify_detects_corruption ... ok

Ran 4 tests in 0.107s
OK
```

这组结果是在 Python 3.12.7 的开发环境中得到的，证明纯 Python 核心逻辑可运行，**不能替代 openEuler 上的 systemd、权限和文件系统验证**。发布前应在你的 openEuler 实例补充：

| 环境 | Python | 文件系统 | 样本规模 | backup | verify | restore |
|---|---|---|---:|---:|---:|---:|
| openEuler **[版本]** | **[版本]** | **[ext4/Btrfs]** | **[文件/GB]** | **[秒]** | **[秒]** | **[秒]** |

同时记录峰值内存和平均吞吐量。测试集必须同时包含大量小文件和少量大文件，因为两者的瓶颈不同。

## 11. 威胁模型与限制

HomeVault v0.1 明确不解决以下问题：

- **磁盘整体丢失**：源和 Vault 在同一台机器上时无法抵御失窃、火灾；
- **勒索软件**：服务账号若长期可写 Vault，攻击者可能破坏备份；
- **机密性**：对象没有加密，拥有磁盘读取权限的人可以读取内容；
- **恶意篡改证明**：SHA-256 不能阻止攻击者同步修改对象和数据库；
- **应用一致性**：不能直接备份正在写入的数据库；
- **保留策略**：当前版本不自动清理旧快照和孤立对象。

真正的家庭备份仍应遵循 3-2-1：至少三份数据、两种介质、一份异地。HomeVault 可以承担其中一份可审计的本地快照，但不应成为唯一副本。

## 12. 值得继续贡献的方向

- 对对象进行分块，避免大文件小改动后完整复制；
- 加入读取前后 stat 校验，检测扫描期间变化；
- 为 SQLite 元数据生成带密钥的完整性清单；
- 支持 S3/OBS 异地副本与服务端加密；
- 提供 `gc --dry-run` 和保留期策略；
- 输出 Prometheus 文本指标；
- 在 Btrfs 快照上完成一致性备份；
- 增加故障注入测试：磁盘满、进程被杀、数据库锁、对象丢失。

这些任务都可以拆成独立 Issue，并附上验收条件。开源项目的价值不只是“代码可见”，而是别人能理解它的保证、限制和下一步。

## 13. 小结

HomeVault 最重要的不是 SHA-256 或 SQLite 本身，而是把备份拆成可验证的契约：内容写完整后才获得最终名称；路径和内容分离；默认不覆盖；恢复路径必须受限；服务只拥有必要权限；每个对象可以重新校验。

当一篇文章能同时给出源码、测试、失败模式和恢复演练，它才可能成为后来者真正愿意引用的技术资料。

## 参考资料

- [openEuler 官方网站](https://www.openeuler.org/zh/)
- [openEuler 文档中心](https://docs.openeuler.org/)
- [Python hashlib 文档](https://docs.python.org/3/library/hashlib.html)
- [SQLite 事务文档](https://www.sqlite.org/lang_transaction.html)
- [systemd.exec 沙箱选项](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)
