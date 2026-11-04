# Claude · 桌面书灵

一只住在 Windows 桌面上的 Claude 娘：透明、置顶，铜橙色长发、橙花发饰，随身带着一本书。
她会自己散步、挥手、看书、唱歌；你用 Claude 干活时，她会跟着一起翻书、思考、拿放大镜查资料，
脚下的小牌子显示 **Claude 官方 5 小时额度已用百分比**。

<p align="center"><img src="docs/hero.png" width="360" alt="桌面上的 Claude 娘和脚下的用量牌"></p>

> 本项目参照 [dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop)（源自 [PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet)）的结构改写：
> PySide6 透明置顶窗口 + 按状态分类的 AI 视频动作 + 托盘与右键菜单。在此基础上换成了 CrossPet 的 Claude 角色，
> 并加入了与 Claude Code / Claude 桌面版的实时联动。感谢原作者们！

## 动作一览

![动作一览](docs/actions.png)

全部画面都是用同一张立绘做首尾帧生成的 10 秒绿幕视频，抠像后按原帧率播放，动作之间无缝衔接，没有静态贴图跳变。

| 场景 | 她在做什么 |
|---|---|
| 闲着 | 待机呼吸，随机挥手 / 看书 / 思考 / 唱歌 / 拿放大镜，或者散步（关掉重力后会斜着走） |
| 拖拽、甩出去 | 跟着鼠标弹簧摆动，碰墙反弹，落地挤压 |
| 摸头、单击、双击 | 冒爱心、鼓脸、欢呼 |
| 右键 →「✨ 动作」 | 只列出已经有视频的动作，加上「散步」 |
| Claude 在干活 | 读文件看书、搜索拿放大镜、思考托腮，一轮完成后挥手庆祝；干活期间不乱跑、不睡觉 |

## 快速开始

需要 Windows 10/11 和 Python 3.10+。

1. 下载本仓库，双击 **`setup.bat`**：自动建虚拟环境、安装依赖并启动。
2. 以后双击 **`run.bat`** 打开（不会弹控制台）。也可以在右键菜单勾选「开机自启」。

手动方式：

```bash
pip install -r requirements.txt
python -m aoi_pet
```

程序是单实例的，重复启动会把她召唤到屏幕中央。启动出错时会弹窗提示，日志在 `%APPDATA%\ClaudePet\error.log`。

## 连接 Claude

右键 →「🔗 连接 Claude」→ 勾选「接入 Claude Code」，或运行 `python tools/install_claude_hooks.py install`。之后**新开**的会话生效。

| 你在用 | 怎么连 | 能知道什么 |
|---|---|---|
| Claude 桌面版的 **Code** 标签页 | Claude Code mod（`integrations/claude_mod`） | 正在做什么 + 官方 5 小时 / 每周百分比 |
| 终端里的 `claude` | 钩子 + 状态栏脚本 | 正在做什么 + 官方百分比 |
| Claude 桌面版的普通聊天 | 只看进程和前台窗口 | 你在用它时她安静陪着，不乱跑 |
| 以上都没有 | 读本机日志 `~/.claude/projects` | 今日 / 本时段 token 用量（不用接入） |

- **只碰自己的设置**：接入会改 `~/.claude/settings.json`，改前自动备份，只增删本桌宠自己的条目；菜单里取消勾选即可撤销。
- **隐私**：钩子和 mod 只记录事件名、工具名、时间和额度百分比，不读、不写提示词、回复或文件内容；桌宠本身不联网。
- 牌子第一行「5小时已用 42% · 16:00 重置」，第二行「本周已用 18%」；任一窗口超过 90% 时牌子变红，她会露出累了的样子。鼠标悬停在牌子上能看到 token 明细。
- 状态文件与 [CrossPet](https://github.com/lokicorvus/crosspet) 同格式同位置（`%LOCALAPPDATA%\CrossPet\state`），两边可以互通。

## 自己做新动作

1. 按 [`video_prompts/提示词.md`](video_prompts/提示词.md)，用 `video_prompts/claude_idle_greenscreen.png` 当首帧和尾帧，用图生视频工具生成 10 秒绿幕视频；
2. 按文档里的文件名放进 `video_prompts/videos/`（比如 `dance.mp4`、`walk_left.mp4`）；
3. `pip install -r requirements-tools.txt` 后运行 `python tools/import_videos.py`。

新动作会自动出现在右键菜单和随机动作里。抠像、配准、播放的细节见 [docs/技术说明.md](docs/技术说明.md)。

## 代码结构

```text
aoi_pet/                 桌宠本体
  app.py                 单实例、托盘、启动
  window.py              透明窗口、拖拽与抛掷物理、走路、用量牌、右键菜单
  brain.py               行为状态机（待机 / 散步 / 拖拽 / 下落 / 睡觉 / 动作 / 跟着 Claude 干活）
  claude_painter.py      视频帧加载、预读、交叉淡化、转身
  rig.py, physics.py     呼吸、眨眼、弹簧与软链
  bubble.py, particles.py, lines.py   对话气泡、爱心音符等特效、台词
  claude_link.py         汇总各数据来源，生成用量牌、悬停详情和连接状态
  sources/               数据来源：工作状态、官方额度、桌面版进程
  claude_usage.py        统计本机 Claude Code 日志里的 token 用量
  claude_setup.py        接入 / 撤销（钩子、状态栏、mod）
  assets/claude/         立绘、角色设定、视频动作帧（video/）
integrations/            装进 Claude Code 的部分
  claude_hook.py         钩子：工作状态 → claude-state.json
  claude_statusline.py   状态栏：官方额度 → claude-quota.json
  claude_mod/            mod：桌面版 Code 标签页的工作状态和官方额度
tools/                   素材导入、截图和自查工具
video_prompts/           生成视频用的提示词和首帧图
```

## 测试与自查

```bash
python tools/interaction_test.py   # 离屏模拟拖拽、甩出、摸头、点击，拼成一张图
python tools/screenshots.py        # 重新生成 docs/ 里的截图，并检查用量牌没有被屏幕下沿切掉
python tools/motion_check.py out   # 60fps 逐帧检查动作里有没有「抽一下」的突变帧
```

## 致谢与授权

- **[MerZlin/dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop)**（MIT）及其上游 **[PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet)**：
  本项目的整体结构（透明窗口、视频动作分类、托盘与菜单）参照它们改写。
- **[lokicorvus/crosspet](https://github.com/lokicorvus/crosspet)**：Claude 角色立绘、角色设定、部分台词，以及与 Claude Code 联动的状态文件格式。
- 代码以 [MIT](LICENSE) 发布。角色立绘、由立绘生成的视频动作帧和角色设定遵循 **CC BY-NC-SA 4.0**（署名、非商业性使用、相同方式共享），
  详见 [`aoi_pet/assets/claude/LICENSE.md`](aoi_pet/assets/claude/LICENSE.md)。
- 本项目为非官方同人作品，与 Anthropic 无关；Claude 是相应权利人的商标。
