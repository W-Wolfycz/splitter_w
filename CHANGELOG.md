# 更新日志

## 2.2.0
> 2026-07-08

- LLM 分段支持含非 Plain 组件的 chain（如 At），自动处理最长 Plain
- 颜文字保护合并为单一配置项 `emoticon_protection`

## 2.1.1
> 2026-07-07

- LLM 分段未应用时（chain 含非 Plain 组件等情况）不再执行 `clean_before_regex` 前置过滤

## 2.1.0
> 2026-07-06

- 修复 `log_with_bot_id` 误用 `get_platform_id()`，改为 `get_self_id()` 以正确区分多 Bot 实例
- 修复 `split_scope=all` 时 LLM 辅助分段被静默跳过的问题
- 修复单段输出路径跳过 `_mark_bot_reply`，导致 smart_reply 状态在单/多段分支下不一致
- 修复 `linear` 延迟策略无上限，长文本可能产生 10s+ 延迟
- `_MAX_LOG_DELAY` 重命名为 `_MAX_DELAY`，统一应用于 log/linear 策略
- 新增颜文字保护：分段 LLM 提示词增加 ``` 包裹颜文字的处理规则；新增 `strip_emoticon_wrappers` 配置项（默认开启），分段归一化后自动剥离反引号

## 2.0.0
> 2026-06-13

- 统一所有日志前缀为 `[SplitterW]`
- 新增 `log_config` 配置组（替换原 `basic_settings.debug_mode`）：
  - `log_with_bot_id`：日志前缀附加机器人实例 ID
  - `debug_to_info`：debug 日志提级为 info 输出

---

## 1.2.0
> 2026-05-25

1. 分段点交由 LLM 通过 `\n` 控制，插件仅负责切分和发送
2. 仅处理 `\n` 和 `\`+`n` 两种分段标记
3. 移除正则分段、符号保护对、碎片段抑制、代码块保护等复杂逻辑，精简绝大部分代码

## 1.1.0
> 2026-05-21

1. 碎片段处理重构：将碎片段合并逻辑从后处理移至切分阶段
2. 移除碎片段后处理合并逻辑，保持代码简洁
3. 修复默认分段正则 `r"\\n"`（双反斜杠）不匹配换行符的问题，修正为 `r"\n"`

## 1.0.0
> 2026-05-18

1. 移除 Simple 分段模式及其相关配置，仅保留正则分段
2. 移除配置迁移逻辑
3. 新增符号保护对配置
4. 新增保护对分段阈值配置
5. 重构回复逻辑：enable_reply 总开关 + enable_smart_reply 子选项
6. 移除 _remove_reply_components 方法
7. 将内置硬编码暴露到配置文件 default 中
8. 正则引擎从 re 换为 regex
