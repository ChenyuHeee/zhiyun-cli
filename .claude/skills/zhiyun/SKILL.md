---
name: zhiyun
description: 导出浙江大学智云课堂课程回放的字幕文字稿和课件图片。当用户提到智云课堂、课程回放、课堂字幕、课件/PPT、复习某节课、下载回放内容时使用。
---

# 智云课堂回放导出

本 Skill 使用仓库里的 Python CLI，把浙江大学智云课堂某节课导出为带时间轴的文字稿和课件图片。

## 定位 CLI

```bash
ZHIYUN_REPO="${ZHIYUN_REPO:-$HOME/projects/zhiyun}"
cd "$ZHIYUN_REPO"
```

如果 `zhiyun` 在 PATH 上，也可以直接用 `zhiyun`。如果仓库在别处，先定位包含 `pyproject.toml` 和 `zhiyun/` 的目录并 `cd` 过去；不确定时问用户。

## 用户只需要提供

1. 智云学号
2. 统一身份认证密码
3. 想导出的课程/讲次（不确定就先列出来让用户选）

## 首次登录 / 凭证过期

先检查已有凭证：

```bash
python3 -m zhiyun status --check
```

如果 `.env` 不存在或凭证无效，向用户要学号/密码，在仓库根目录写 `.env`：

```dotenv
A = <学号>
P = <密码>
```

然后：

```bash
chmod 600 .env
python3 -m zhiyun login
```

成功标志：“验证通过：当前账号可访问 N 门课程”。

- 验证码/二次验证导致自动登录失败时：
  ```bash
  python3 -m zhiyun login --browser --force
  ```
  让用户在打开的浏览器里完成登录。
- 不要把密码复述给用户，不要把 `.env` 提交到 git。
- JWT 约 1 天有效；过期后重新 `login` 即可，`.env` 已保存则无需再问用户密码。

## 工作流

```bash
# 1. 课程
python3 -m zhiyun courses --json

# 2. 课时（优先 has_transcript=true；用户说最新就用 --lecture last）
python3 -m zhiyun lectures "<课程名或ID>" --json

# 3. 导出
python3 -m zhiyun export "<课程>" --lecture last --quiet
# 也支持 --lecture N / --sub-id ID / --title 关键字
# 整门课：--all；纯文字：--transcript-only；不下载图片：--no-images
```

输出默认在 `./zhiyun_output/<课程>/<日期_课时>/`，包含：

- `lecture.md`：课件图片 + 字幕交错时间轴
- `transcript.md`：分段文字稿 + 逐句时间轴
- `transcript.txt`：连续文本
- `transcript.json`
- `slides/`：课件图片
- `metadata.json`：统计、回放地址、备注

## 汇报给用户

- 课程 / 课时 / 日期
- 输出绝对路径
- 字幕段数、课件张数
- 提醒：字幕是平台 ASR 原文，可能有错字和课前闲聊；课件图是平台截屏，可能混入桌面/浏览器画面
- 用户要总结时，读取 `transcript.md` 后再总结；不要直接粘贴整份字幕到对话里

## 错误处理

- 403 / 凭证过期 → `python3 -m zhiyun login --force`
- 课程列表为空 → 让用户提供课程 ID（课程详情页 URL 的 `course_id=`）
- 课时无字幕 → 加 `--try-transcript`；仍为空就只导课件
- 图片失败 → 重新登录后加 `--force` 重试
- 接口异常 → 导出时加 `--save-raw` 保存原始 JSON

## 限制

非官方工具；仅导出用户本人有权访问的课程，仅供个人学习，不要传播课程内容或批量抓取。
