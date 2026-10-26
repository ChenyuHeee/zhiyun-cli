# 智云课堂回放导出（zhiyun-cli）

把浙江大学智云课堂某节课的回放整理成：

- `transcript.md`：按段落整理的文字稿，带逐句时间轴
- `transcript.txt`：连续文本，方便直接复制
- `slides/`：课件图片（按出现时间命名）
- `lecture.md`：课件图片与字幕按时间轴交错排列，适合复习
- `metadata.json`：课程、课时、回放地址、字幕/课件数量等

只使用自己的浙大账号读取本人可访问的课程，不做视频下载，不绕过任何权限控制。

仓库地址：<https://github.com/ChenyuHeee/zhiyun-cli>。

## 给 AI Agent / Claude Code Skill

除了人类可用的 CLI，这个仓库也是一份可直接交给 Agent 的工具包。

- Agent 操作手册：[AGENTS.md](AGENTS.md)
- 可直接使用的 Claude Code Skill：[`.claude/skills/zhiyun/SKILL.md`](.claude/skills/zhiyun/SKILL.md)

用户只需要提供 **智云学号、统一身份认证密码**，以及想导出的课程/讲次。Agent 会：

1. 在本地写 `.env`（已被 `.gitignore` 忽略）
2. 运行登录
3. 列出课程/课时
4. 导出文字稿和课件图片
5. 返回输出路径和统计

在任意目录启用这个 Skill：

```bash
cd /path/to/zhiyun-cli
mkdir -p ~/.claude/skills
ln -sfn "$PWD/.claude/skills/zhiyun" ~/.claude/skills/zhiyun
export ZHIYUN_REPO="$PWD"    # 建议写进 ~/.zshrc
```

然后把类似这样的话交给 Agent：

> 帮我导出《课程名》最新一讲的回放，文字稿和课件图片都要。

Agent 首次会问学号/密码，之后复用 `.env`；凭证大约一天过期，过期后 Agent 自己重新登录即可。

## 安装

需要 Python 3.9+。仓库可以直接用系统 Python 运行，也可以放在独立虚拟环境里：

```bash
git clone https://github.com/ChenyuHeee/zhiyun-cli.git
# 或使用 SSH：git clone git@github.com:ChenyuHeee/zhiyun-cli.git
cd zhiyun-cli
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

如果你已经在本地 checkout，直接 `cd` 到仓库根目录执行后面的命令即可。

如果想全局使用，可以：

```bash
pipx install --editable .
```

登录兜底需要 Playwright（可选）：

```bash
pip install -e '.[browser]'
python -m playwright install chromium
```

安装后命令是 `zhiyun`；在项目目录内也可以直接用 `python -m zhiyun`。

## 快速开始

```bash
# 1. 登录（会自动走统一身份认证，密码输入时不显示）
zhiyun login

# 2. 看看自己的课程
zhiyun courses

# 3. 看某门课的课时
zhiyun lectures 数据结构

# 4. 导出最新一节有回放的课
zhiyun export 数据结构 --lecture last
```

默认输出到当前目录的 `zhiyun_output/`：

```text
zhiyun_output/
└── 数据结构/
    └── 2024-09-12_第3讲/
        ├── lecture.md       # 图片 + 字幕的时间轴
        ├── transcript.md    # 分段文字稿
        ├── transcript.txt   # 连续文本
        ├── transcript.json
        ├── slides/
        │   ├── 001_at_00-00-00.jpg
        │   └── 002_at_00-05-12.jpg
        └── metadata.json
```

## 常用命令

```bash
zhiyun courses --keyword 算法          # 按课程名/教师筛选
zhiyun lectures 数据结构 --available   # 只看有字幕（可回放）的课时

zhiyun export 数据结构 --lecture 3     # 按 lectures 里的序号
zhiyun export 数据结构 --lecture last  # 最新一节有字幕的课时
zhiyun export 123456 --sub-id 789012   # 直接用课程 ID / 课时 ID
zhiyun export 数据结构 --title 期中复习 # 按标题关键字

zhiyun export 数据结构 --all           # 导出所有有字幕的课时
zhiyun export 数据结构 --lecture last -o ~/zju/notes
zhiyun export 数据结构 --lecture last --transcript-only
zhiyun export 数据结构 --lecture last --no-images
zhiyun export 数据结构 --lecture last --try-transcript
zhiyun export 数据结构 --lecture last --force --save-raw
```

`--all` 只会导出被平台标记为“有字幕”的课时；标记为无字幕的课时可以用 `--sub-id` 单独尝试，并加 `--try-transcript`。

## 登录与凭证

### 从 `.env` 读取账号密码

支持把学号/密码放在本地 `.env`，不用在命令行里输入：

```dotenv
# 单行写法（按你的习惯）
A = 3230xxxxxxxx; P = your-password;

# 或标准写法
ZHIYUN_USERNAME=3230xxxxxxxx
ZHIYUN_PASSWORD=your-password
```

也接受 `ZJU_USERNAME` / `ZJU_PASSWORD` / `PASSWORD` / `TOKEN` 等常见别名。
然后直接登录：

```bash
zhiyun login
```

工具会自动读取当前目录的 `./.env`，也会读 `~/.config/zhiyun/.env`；
也可以显式指定：

```bash
zhiyun login --env-file /path/to/.env
# 或
export ZHIYUN_ENV_FILE=/path/to/.env
```

注意：

- 真实环境变量优先于 `.env`，不会被 `.env` 覆盖
- `.env` 已在 `.gitignore` 中，不会被提交
- 建议 `chmod 600 .env`，文件权限过松时工具会提醒
- 密码里如果有 `;`，请用引号包起来，例如 `P = "a;b"`
- 修改 `.env` 里的密码后，用 `zhiyun login --force` 重新登录
- 默认只把 JWT 保存到配置文件，不会把密码写进去

### 直接登录

默认方式复现浙大统一身份认证的纯 HTTP 登录流程（CAS RSA + 智云 OAuth），不启动浏览器。

- 凭证保存在 `~/.config/zhiyun/config.json`，权限尽量设为 `0600`
- 默认**不保存密码**；凭证过期后重新运行 `zhiyun login`
- 如果希望以后自动复用密码，可加 `--save-password`，密码会明文保存在
  `~/.config/zhiyun/credentials.json`（同样 `0600`）。请只在自己的电脑上使用
- 统一认证要求验证码、扫码，或自动登录失效时：

```bash
zhiyun login --browser
```

会打开浏览器窗口，你手动完成登录（密码或扫码都可以），工具自动抓取凭证并关闭窗口。

手动设置 JWT（适合高级用户/调试）：

```bash
# 从浏览器开发者工具复制 Authorization: Bearer ... 里的 JWT
zhiyun login --token 'eyJ...'
# 或从标准输入读取，避免留在 shell 历史
zhiyun login --token-stdin
```

查看状态：

```bash
zhiyun status
zhiyun status --check   # 真正请求一次接口，验证凭证和权限
zhiyun logout
zhiyun logout --keep-credentials
```

自动化脚本可以使用 `.env` 或环境变量，避免把密码写进命令行：

```bash
export ZHIYUN_USERNAME=3230xxxxxx
export ZHIYUN_PASSWORD='...'
zhiyun login
```

也支持 `ZHIYUN_TOKEN`、`ZHIYUN_STUDENT_ID`、`ZHIYUN_ENV_FILE` 和 `ZHIYUN_CONFIG_DIR`。

## 常见问题

**提示 `HTTP 403 Auth Forbidden` / 字幕或课件接口 403**

凭证失效或没有权限。先运行 `zhiyun status --check`，必要时 `zhiyun login --force`。

**课程列表为空**

接口返回空可能是权限或平台接口调整。可以直接用课程 ID：

```bash
zhiyun export 123456 --lecture last
```

课程 ID 可以在智云课堂课程详情页 URL 的 `course_id=` 后找到。

**课时没有字幕**

平台只为部分回放生成语音识别字幕。`lectures` 里显示“无字幕”的课时通常还没有转写；可以加 `--try-transcript` 尝试。课件图片有时仍然可用。

**图片下载失败**

单张失败不会中断整节课，失败信息会写入 `metadata.json` 的 `notes` 和 `slides[].error`。如果是 403，重新登录后加 `--force` 重试。

**校外网络**

如果连不上 `classroom.zju.edu.cn` 或 `yjapi.cmc.zju.edu.cn`，先连接学校 WebVPN。本工具目前不内置 WebVPN 转发。

**自动登录失败**

学校可能临时启用验证码/二次验证，或调整了登录流程。使用 `zhiyun login --browser` 即可；也可以 `--force` 重试。

## 说明与限制

- 这是非官方工具，依赖智云课堂当前的内部接口；平台改版后可能需要更新。
- 不下载回放视频。`metadata.json` 里会保留 `playback_url`，方便自行在合法范围内使用。
- **课件图是平台按时间截取的屏幕帧**（通常是 1280×720），不是从 PPT 源文件导出的。老师切换到桌面、浏览器、视频时，可能出现与课件无关的画面；这些帧默认保留，以保证时间轴完整。
- **字幕是平台 ASR 原文**，包含课前闲聊、语气词和识别错误。工具只做了连续重复去重、空白清理、按时间分段，不做语义改写或纠错。
- 智云 JWT 有效期通常约一天。过期后重新运行 `zhiyun login` 即可；如果用了 `.env`，不需要再次输入密码。
- `zhiyun logout` 只清除本地 JWT 和工具保存的密码，**不会删除项目里的 `.env`**。
- 请仅导出自己有权访问的课程，仅供个人学习使用，不要公开传播、批量抓取或用于商业用途。

## 仓库结构

```text
zhiyun/                     # CLI 源码
tests/                      # 单元测试
.claude/skills/zhiyun/      # 内置 Agent Skill
AGENTS.md                   # 给 Agent 的完整操作手册
CLAUDE.md                   # Claude Code 项目入口
.env.example                # 凭证模板（.env 被 gitignore）
README.md
```

## 开发

```bash
python3 -m unittest discover -s tests -v
```

主要模块：

```text
zhiyun/auth.py      # CAS RSA 登录、智云 OAuth、Playwright 兜底
zhiyun/api.py       # 课程、课时、字幕、课件接口
zhiyun/export.py    # 文字稿、图片下载、Markdown 时间轴
zhiyun/cli.py       # 命令行交互
```

## License

MIT

