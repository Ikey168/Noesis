/** Trusted server-side init-page hook. Never load a hook supplied by a source. */
import { lookup } from 'node:dns/promises';
import { isIP } from 'node:net';

export function publicAddress(address) {
  if (isIP(address) === 4) {
    const [a, b, c] = address.split('.').map(Number);
    return !(a === 0 || a === 10 || a === 127 || a >= 224 ||
      (a === 100 && b >= 64 && b <= 127) || (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b <= 31) || (a === 192 && (b === 0 || b === 168)) ||
      (a === 198 && (b === 18 || b === 19 || (b === 51 && c === 100))) ||
      (a === 203 && b === 0 && c === 113));
  }
  if (isIP(address) === 6) {
    const a = address.toLowerCase();
    return !(a === '::' || a === '::1' || a.startsWith('::ffff:') || a.startsWith('fc') ||
      a.startsWith('fd') || /^fe[89ab]/.test(a) || a.startsWith('ff') || a.startsWith('2001:db8'));
  }
  return false;
}

export default async ({ page }) => {
  const requested = JSON.parse(process.env.NOESIS_BROWSER_ALLOWED_ORIGINS || '[]');
  if (!Array.isArray(requested) || requested.length < 1 || requested.length > 20)
    throw new Error('Explicit Noesis public origin allowlist required');
  const origins = new Set(requested.map(value => {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.username || url.password || url.port || url.origin !== value)
      throw new Error('Use canonical public HTTPS origins, with no credentials or port');
    return value;
  }));
  const context = page.context();
  if (context.__noesisGuardInstalled) return;
  context.__noesisGuardInstalled = true;
  let requests = 0;
  // A new isolated context starts a fresh finite network budget.
  await context.route('**/*', async route => {
    const request = route.request();
    try {
      const url = new URL(request.url());
      if (++requests > 200 || !['GET', 'HEAD'].includes(request.method()) ||
          !origins.has(url.origin) || url.username || url.password || request.redirectedFrom())
        return await route.abort('blockedbyclient');
      const addresses = await lookup(url.hostname, { all: true });
      if (!addresses.length || addresses.some(item => !publicAddress(item.address)))
        return await route.abort('blockedbyclient');
      const response = await route.fetch({ maxRedirects: 0, maxRetries: 0, timeout: 10000 });
      if (response.status() >= 300 && response.status() < 400)
        return await route.abort('blockedbyclient');
      const headers = response.headers();
      if (Number(headers['content-length'] || 0) > 4000000 || headers['content-disposition']?.includes('attachment'))
        return await route.abort('blockedbyclient');
      const body = await response.body();
      if (body.length > 4000000) return await route.abort('blockedbyclient');
      await route.fulfill({ response, body });
    } catch {
      await route.abort('failed').catch(() => {});
    }
  });
  if (context.routeWebSocket)
    await context.routeWebSocket('**/*', socket => socket.close());
  context.on('page', child => child.on('download', download => download.cancel()));
  page.on('download', download => download.cancel());
};
