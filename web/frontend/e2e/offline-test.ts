import { test as base, expect } from '@playwright/test';

export const test = base.extend({
  context: async ({ context }, use) => {
    if (process.env.BINGGO_OFFLINE === '1') {
      await context.route('**/*', async route => {
        const host = new URL(route.request().url()).hostname;
        if (['127.0.0.1', 'localhost', '[::1]'].includes(host)) await route.continue();
        else await route.abort('blockedbyclient');
      });
    }
    await use(context);
  },
});
export { expect };
