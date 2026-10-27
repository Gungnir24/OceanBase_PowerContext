# 项目网站（Qoder Sites）

本目录是发布到 Qoder Sites 的静态站点，`index.html` 为入口，`screenshots/` 为页面截图。

## 发布步骤

1. 打开 Qoder 客户端 → **Sites** → 新建站点；
2. 选择「上传静态文件」，把本目录（`qoder/site/`）整体上传，入口设为 `index.html`；
3. 发布后得到形如 `https://<site>.qoder.site` 的链接；
4. 把链接填到：
   - 提交表单的「作品链接」；
   - 本目录 `index.html` 底部的说明（可选）；
   - `docs/qoder-development-log.md` 第 6.3 节。

## 本地预览

```bash
cd qoder/site
python -m http.server 8080
# 打开 http://127.0.0.1:8080
```

## 需要替换的内容

- 底部「作品链接见提交表单」：发布后替换为实际 URL；
- 若最终 commit 与仓库当前 HEAD 不同，更新页脚的分支/commit 文案；
- 截图如更新，同步替换 `screenshots/` 下的文件。

> 站点只做展示，不包含后端；在线体验需要按 README 在本地启动 seekdb、PowerContext
> 与应用。演示模式可用 `python app/server.py --demo` 直接预览界面。
