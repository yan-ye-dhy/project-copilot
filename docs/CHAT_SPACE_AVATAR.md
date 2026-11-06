# 聊天空间与原创剑客头像

## 1. 目标与范围

基于 main `33bb40a` 的纯前端改动：将对话放回页面中心，保留暖白纸张、竹青和水墨装饰。桌面顶栏64px、手机60px，模式、简短额度、任务中心和账号菜单集中在一条操作栏；欢迎介绍只存在于空白欢迎页。

没有修改Python、数据库、迁移、Agent/RAG/Worker、`api.js`、Cloud Function、代理密钥、发布授权或配额，也没有运行Docker、Tunnel、真实业务请求或付费DeepSeek/智谱调用。没有改变正在测试的任务。

## 2. 修改文件

| 文件 | 职责 |
| --- | --- |
| `src/project_copilot/web/index.html` | 紧凑顶栏、额度/账号弹层、默认关闭的任务抽屉 |
| `web/assets/styles.css` | 单一聊天滚动区域、响应式空间、头像与任务列表样式 |
| `web/assets/app.js` | 抽屉交互、只读状态查询、隐藏/恢复、输入框自适应高度 |
| `web/assets/task-center.js` | 本地恢复记录白名单、真实状态标签和可逆隐藏规则 |
| `web/assets/avatar-swordsman.webp` | 账号和用户消息共用的原创头像 |
| `scripts/build_edgeone.mjs` | 显式允许构建新模块和头像，不放宽其他资产白名单 |
| `tests/edgeone_build.test.mjs` | 头像完整性、缺失时失败及静态构建边界 |
| `tests/frontend_task_center.test.mjs` | 元数据、真实状态和隐藏规则单元测试 |
| `tests/frontend_space_browser.test.mjs` | 从实际静态产物加载的三尺寸浏览器验收 |
| `tests/frontend_{auth,visual}_browser.test.mjs` | 原登录、恢复、Markdown与异常状态回归 |

## 3. 核心体验

顶栏只显示已用/总任务数，例如`37 / 500`。点击额度可查看剩余授权时间、占槽及执行器状态。任务中心默认完全收起，不占聊天正文和输入框空间。输入框从52px起增长，桌面最多176px、手机最多128px；超长内容在输入框内滚动，聊天正文拥有独立滚动区。

任务中心保留原任务编号、类型和本机提交记录时间；服务器现有GET响应不提供创建时间，因此不将本机时间冒充数据库时间。状态仅来自编号匹配的真实`GET /jobs/{id}`，未读取时显示“尚未查询”，不按等待时长推断运行或失败。每次打开最多顺序读取8条，其余可手动查询；关闭面板中止的仅是面板GET，不是Worker或原任务。

查询/恢复仍调用原只读恢复能力，保留SSE进度、50分钟等待、停止等待和最终Schema/引用/提案验证；不会重新`POST /jobs`。查看提案也不会自动确认。刷新会清除内存Context凭据，既有提案权限边界保持原样。

## 4. 隐藏、隐私与无障碍

- 只有从GET确认的`failed`或`unknown`记录提供“隐藏此记录”；“查看已隐藏”中可取消隐藏，不删除后端任务、费用、幂等或额度。
- 本地记录按已登录用户ID隔离，最多保留最近32条、7天；只保存任务ID、类型、本机提交时间、零起始游标和隐藏标志，不保存状态、问答、模型草稿、Context Token、Cookie、CSRF或密钥。
- 退出仅清除内存内容和凭据，保留各账号本机任务索引以便重新登录恢复；新账号不会渲染或查询旧账号记录。用户应在共用设备使用结束后自行清理浏览器网站数据，清理后也不会删除后端任务。
- 没有新增“永久删除记录”按钮。成功恢复原结果后仍按既有逻辑移出恢复列表；列表保留期或上限导致的本机索引淘汰不改变后台历史。
- 原生模态dialog配合显式Tab/Shift+Tab焦点环，支持Esc、关闭按钮、遮罩点击和焦点恢复。键盘关闭任务中心不触发停止等待。

## 5. 头像资源与制作

使用内置imagegen生成原创角色，不使用官方角色肖像、网络图片、CDN、外部字体或图床。素材为黑发束髻、青灰长袍、肩后剑柄、沉静年轻剑客的水墨细绘像；未要求或使用任何既有角色的脸。最终资源为`src/project_copilot/web/assets/avatar-swordsman.webp`，256×256、13,174字节（约12.9KiB），通过本机浏览器画布进行尺寸/格式优化，无EXIF。原始大图未进入构建产物。用户头像与账号头像共用该资源，助手仍为绿色“知”字印章。

最终生成提示词（内置工具；不是DeepSeek/智谱业务调用）：

> Use case: illustration-story. Asset type: an original square portrait avatar for a refined Chinese ink-and-paper web chat interface, readable when cropped to a circle at 36–48 pixels. Generate one original young adult East Asian wandering swordsman, calm, clear-eyed, reserved, kind but self-possessed. Distinctive refined realistic illustrated face, three-quarter frontal head-and-shoulders composition, very dark black hair tied in a traditional high bun with a few naturally loose strands, simple bamboo-green and grey-white historical Chinese robe with folded collar. A small understated sword hilt peeks behind one shoulder. Fine hand-painted character portrait blended with restrained Chinese ink washes, not flat icon art. Warm ivory rice-paper background, pale sage and bamboo-green accents. Face occupies enough of the central image for tiny avatar use, eyes clear, soft natural illumination, uncluttered silhouette, subtle brush texture. An original character, do not resemble or copy any existing movie, animation, comic, celebrity or official character portrait. No text, no seal, no logos, no watermark, no frame, no glow, no magical effects, no game concept-art excess. Square composition with margins safe for circular crop.

## 6. 自动验证与截图

所有浏览器API请求均被Playwright拦截，模型、工单、提案、登录和状态数据为合成夹具，不是公网业务执行证据。预览先调用真实`build_edgeone.mjs`生成独立临时目录，再读取产物，不从源码假装部署成功。中文视觉复核只读取本机已有字体，字体不入库也不加入构建。

| 本轮实际验证 | 结果 |
| --- | --- |
| `node --test --test-isolation=none tests/*.test.mjs` | 263通过、0失败、0跳过，41.93秒；配置已有Playwright和本机字体路径 |
| `PYTHONPATH=src:. .venv/bin/python -m pytest -q tests/test_web.py` | 13通过、0失败，1.37秒；测试禁止构造数据库或模型服务 |
| 其中任务中心纯函数规则 | 12项，已包含在263项中，不重复相加 |
| 其中实际构建的三尺寸浏览器验收 | 9项，已包含在263项中 |
| 最终列表样式定稿后重跑上述浏览器子集 | 9通过、0失败、0跳过，19.65秒；不是另外9项不同测试 |
| JS语法 / `git diff --check` | 通过 |

首次受限沙箱无法正常启动Chromium及完成ASGI测试，构建子进程的异常输出也受限；改用获准的本机进程环境后上述测试通过，没有通过修改安全断言规避失败。

截图涵盖1440×900、1280×800、390×844：长Markdown、表格、横向代码块、完整提案、32条恢复记录、头像、输入框增长、关闭/隐藏、焦点和账号隔离。

| 视口 | 实际顶栏高度 | 实际聊天滚动区域高度 |
| --- | --- | --- |
| 1440×900 | 64px | 686px |
| 1280×800 | 64px | 586px |
| 390×844 | 60px，另有50px导航 | 约590px |

合成预览：[桌面回答](evidence/CHAT_SPACE_DESKTOP.png)、[笔记本完整提案](evidence/CHAT_SPACE_NOTEBOOK_PROPOSAL.png)、[手机提案](evidence/CHAT_SPACE_MOBILE.png)、[32条任务抽屉](evidence/CHAT_SPACE_TASK_CENTER.png)、[手机任务中心](evidence/CHAT_SPACE_MOBILE_TASKS.png)。

## 7. Git与部署影响

独立分支`feat/chat-space-swordsman`，PR指向main；实际提交号、PR链接和最终测试数在交接时提供。本轮不合并、不部署、不重启服务、不调整私有环境变量或重新授权。

**现有EdgeOne公网前端不需要重建后端镜像。** 本次只更新静态产物，构建命令仍为`node scripts/build_edgeone.mjs`，输出仍为`dist`。用户审核合并后，可由已配置的main生产自动部署构建并更新前端，Cloud Function及后端保持原版本；本轮没有访问或更改云端控制台。是否实际部署成功以用户控制台和新页面为准。

若之后希望从本机Docker的`127.0.0.1:8002`也看到相同界面，该镜像包含其构建时的静态文件，需要另行更新镜像；这不是本次EdgeOne更新的前置条件。本次没有修改后端代码或入口审查文件，既有代码/发布指纹机制未变，不因静态更新要求重新授权。

## 8. 后续验证与限制

审核PR时查看合成截图；合并后刷新EdgeOne页面核对顶栏、头像与任务中心即可，不需要为了验证样式提交付费任务。已有任务恢复仍是GET。公网真实SSE、实际任务执行和浏览器软键盘均未在本次自动化中复测；手机截图为Chromium的390px视口，不能冒充所有手机实机验收。
