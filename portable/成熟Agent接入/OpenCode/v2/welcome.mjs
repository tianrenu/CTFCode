// 展示层引导，不向模型上下文注册工具或注入提示词。
export function overview(state) {
  const phase = { running: "准备中", completed: "已完成", failed: "失败，可查日志", cancelled: "已取消，部分文件保留" };
  return {
    title: "CTFCode · 本地 CTF 工作台",
    placeholder: `模型${state.model_files_present ? "已准备" : "待配置"}  /  题目${state.workspace ? "已打开" : "待选择"}  /  ${state.context}K${state.context === "64" ? "候选" : "默认"}`,
    options: [
      { title: "① 配置本地模型", value: "model", description: state.model_directory + " · 不搬动、不复制权重" },
      { title: "② 校验分片并准备模型", value: "prepare", description: state.model_files_present ? "已有完整pack，可跳过此步" : (phase[state.preparation_state] ?? "只有两个GGUF时执行一次；不启动推理") },
      { title: "③ 打开题目文件夹", value: "cd", description: state.workspace ?? "新建空白会话；旧记录留在原目录" },
      { title: "④ 启动本地模型", value: "start", description: state.model_state === "ready" ? "已经就绪，不重复加载" : "按需加载；保留全部资源保护" },
      { title: "模型下载与使用说明", value: "docs", description: "约58.4GB权重另下；程序、引擎和依赖已经内置" },
      { title: "准备进度 / 失败日志", value: "progress", description: phase[state.preparation_state] ?? "尚未执行模型准备" },
      { title: "资源与运行状态", value: "status", description: "查看实际模型、上下文和资源；状态未知不假装成功" },
      { title: "进入工作台", value: "done", description: "关闭本面板；/ctf 可随时重新打开" },
    ],
  };
}

export function downloads() {
  return ["程序、Python、Strata与分析依赖均已内置。", "",
    "模型：GSQ-RCO Coder IQ1_M，两个分片约58.4GB。",
    "下载源：Hugging Face官方 / hf-mirror.com镜像。",
    "固定直链、revision、SHA256见包内：",
    "文档/模型下载与校验.md", "",
    "① 将两个分片放入模型目录的 IQ1_M 子文件夹。",
    "② 返回开始页选择模型根目录，确认校验与准备。",
    "③ 已有完整packs时跳过准备，打开题目后启动。", "",
    "不必另装Python，不必手改JSON或拼PowerShell命令。",
    "https://github.com/tianrenu/CTFCode"].join("\n");
}
