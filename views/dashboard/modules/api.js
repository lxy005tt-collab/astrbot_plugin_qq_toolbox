// Bridge wrapper around window.AstrBotPluginView.

export class ApiError extends Error {
  constructor(message, status = 0, body = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

export function createApi(bridge) {
  if (!bridge) throw new ApiError("未找到 AstrBotPluginView bridge，请从 AstrBot 后台插件页面打开。");
  const ready = bridge.ready();

  async function request(method, endpoint, payload) {
    await ready;
    let body;
    try {
      body =
        method === "GET"
          ? await bridge.apiGet(endpoint, payload)
          : await bridge.apiPost(endpoint, payload);
    } catch (error) {
      throw new ApiError(error?.message || "请求失败", 0, null);
    }
    if (!body || typeof body !== "object") {
      throw new ApiError("返回格式异常", 0, body);
    }
    if (body.status === "error") {
      throw new ApiError(body.message || "请求失败", 0, body);
    }
    return body.data !== undefined ? body.data : body;
  }

  return {
    ready,
    get: (endpoint, params = {}) => request("GET", endpoint, params),
    post: (endpoint, body = {}) => request("POST", endpoint, body),
    async download(endpoint, params, filename) {
      await ready;
      return bridge.download(endpoint, params, filename);
    },
    context: () => bridge.getContext(),
    getLocale: () => bridge.getLocale(),
  };
}
