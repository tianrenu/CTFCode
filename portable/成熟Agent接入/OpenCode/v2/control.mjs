// 本机控制通道，不注册给模型；这不是对同一Windows用户的OS沙箱。
export async function control(opts, action, value = {}) {
  const cfg = opts.controller;
  if (!cfg) throw new Error("此入口尚未连接模型控制器；不会假装模型已启动");
  const url = new URL(cfg.endpoint);
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || !url.port ||
      ["8080", "18791"].includes(url.port) || url.pathname !== "/" || url.username || url.password ||
      url.hash || url.search || typeof cfg.key !== "string" || !/^[A-Za-z0-9_-]{40,128}$/.test(cfg.key))
    throw new Error("模型控制器地址或认证设置无效");
  if (!["status", "start", "stop", "select", "workspace", "ready", "activity"].includes(action))
    throw new Error("未识别的模型控制操作");
  const response = await fetch(url.origin + "/" + action, { method: "POST", redirect: "error",
    headers: { Authorization: "Bearer " + cfg.key, "Content-Type": "application/json" },
    body: JSON.stringify(value), signal: AbortSignal.timeout(12000) });
  const body = await response.json();
  if (!response.ok || body.ok !== true) throw new Error(body.error ?? "模型控制操作失败");
  return body.data;
}

export function describe(state) {
  const labels = { stopped: "未加载", loading: "正在加载", ready: "已就绪", stopping: "正在卸载", failed: "失败，请查看原因" };
  return ["GSQ-RCO Coder IQ1_M · " + (labels[state.model_state] ?? "未知"),
    "模型目录：" + state.model_directory, "工作目录：" + (state.workspace ?? "尚未选择（/cd）"),
    "会话：" + (state.session ?? "尚未新建"),
    "所选 " + state.context + "K · 实际 " + (state.running_context?.verified ? state.running_context.physical_context / 1024 + "K（已核验）" : "未加载/未核验"),
    "空闲 RAM " + state.available_ram_gib + " GiB" + (state.gpu ? " · 整卡显存 " + state.gpu.gpu_free_mib + " MiB" : ""),
    "模型文件：" + (state.model_files_present ? "必要文件存在，启动仍需检查" : "尚未准备好"),
    ...(state.error ? ["原因：" + state.error] : []),
    ...(state.resource_error ? ["资源采样失败：" + state.resource_error] : []),
    ...(state.metrics_error ? ["引擎采样失败：" + state.metrics_error] : []),
    ...(state.live ? ["原生实时状态：" + JSON.stringify(state.live).slice(0, 1000)] : []),
    ...(state.task ? ["Agent当前任务：" + JSON.stringify(state.task)] : []),
    "", "取消生成≠卸载；旧会话不自动恢复。64K仍是候选。"].join("\n");
}

export function frame(state, session) {
  if (!state) return "CTFCode · 正在连接本地控制器；不会自动加载模型";
  if (state.connection_error) return "CTFCode · 控制器连接失败，运行状态未知；不会自动重启";
  if (session && state.session !== session) return "CTFCode · 历史会话只供查看；/new 或 /cd 新开任务";
  const model = { stopped: "未加载", loading: "加载中", stopping: "卸载中", ready: "已就绪", failed: "加载失败" };
  const task = { idle: "待输入", queued: "已提交", running: "处理中", request: "等待模型首字", generating: "正在生成",
    tool: "执行工具", compacting: "压缩上下文", waiting_permission: "等待操作审批", needs_attention: "需要人工处理",
    progress_gate: "等待调整步骤", succeeded: "本轮结束（答案未认证）", failed: "任务失败", interrupted: "任务已停止" };
  const live = state.live;
  const phase = live?.state === "reading" ? "读入 " + (live.prompt_read ?? "?") + "/" + (live.prompt_tokens ?? "?") + " token" :
    live?.state === "generating" ? "生成 " + (live.generated ?? 0) + " token · " + (live.tok_s_mean ?? "?") + " tok/s" :
      task[state.task?.phase] ?? "待输入";
  return "CTFCode · " + state.context + "K · 模型" + (model[state.model_state] ?? "未知") + " │ " + phase +
    (state.task?.last_tool ? " │ " + state.task.last_tool + " · " + state.task.tools + "次工具" : "") +
    (state.task?.audit_error ? " │ 证据保存异常（/ctf查看）" : "");
}
