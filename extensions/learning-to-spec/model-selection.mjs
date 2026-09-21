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

export async function availableSessionModels(session, { timeoutMs = 3000 } = {}) {
  const modelApi = session?.rpc?.model;
  if (typeof modelApi?.list !== 'function') return [];
  let timer;
  try {
    const result = await Promise.race([
      Promise.resolve().then(() => modelApi.list()),
      new Promise(resolve => { timer = setTimeout(() => resolve(null), timeoutMs); }),
    ]);
    if (!Array.isArray(result?.list)) return [];
    return [...new Set(result.list.map(item => typeof item === 'string' ? item : item?.id)
      .filter(identifier => typeof identifier === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}$/.test(identifier)))];
  } catch {
    return [];
  } finally {
    clearTimeout(timer);
  }
}
