export async function currentSessionModel(session, { timeoutMs = 2000 } = {}) {
  const modelApi = session?.rpc?.model;
  if (typeof modelApi?.getCurrent !== 'function') return null;
  let timer;
  try {
    const result = await Promise.race([
      Promise.resolve().then(() => modelApi.getCurrent()),
      new Promise(resolve => { timer = setTimeout(() => resolve(null), timeoutMs); }),
    ]);
    const modelId = result?.modelId;
    return typeof modelId === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}$/.test(modelId) ? modelId : null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}
