const { test, expect } = require('@playwright/test');

// Synthetic fixture PIN from the verification harness. Not a household PIN.
const FIXTURE_PIN = '246801';

test.use({ viewport: { width: 390, height: 844 } });

test('phone class sheet is nearly full height, scrolls as one, and keeps close pinned', async ({ page }) => {
  await page.goto('/');
  await page.locator('#pinInput').fill(FIXTURE_PIN);
  await page.locator('#unlockBtn').click();
  await expect(page.getByRole('heading', { name: 'Current standing' })).toBeVisible();

  await page.getByRole('button', { name: 'Fixture Beta' }).click();
  await page.getByRole('button', { name: 'Open detail for Science Fixture, A, 91%, no baseline' }).click();

  const drawer = page.locator('#glanceDrawer');
  await expect(drawer).toHaveAttribute('aria-hidden', 'false');
  await expect.poll(async () => (await drawer.boundingBox()).y).toBeLessThanOrEqual(20);
  const viewport = page.viewportSize();
  const box = await drawer.boundingBox();
  expect(box).not.toBeNull();
  expect(box.y).toBeLessThanOrEqual(20);
  expect(box.height).toBeGreaterThanOrEqual(viewport.height * 0.95);
  expect(box.y + box.height).toBeGreaterThanOrEqual(viewport.height - 2);

  const scrolling = await drawer.evaluate((el) => {
    const body = el.querySelector('.glance-drawer-body');
    return {
      drawerOverflowY: getComputedStyle(el).overflowY,
      bodyOverflowY: getComputedStyle(body).overflowY,
      pageOverflow: document.body.style.overflow,
    };
  });
  expect(scrolling.drawerOverflowY).toBe('auto');
  expect(scrolling.bodyOverflowY).toBe('visible');
  expect(scrolling.pageOverflow).toBe('hidden');

  const close = page.locator('#glanceClose');
  const closeBefore = await close.boundingBox();
  expect(closeBefore).not.toBeNull();

  await drawer.evaluate((el) => {
    const body = el.querySelector('.glance-drawer-body');
    const pad = document.createElement('div');
    pad.dataset.e2ePad = '1';
    pad.style.height = '2400px';
    body.appendChild(pad);
    el.scrollTop = el.scrollHeight;
  });

  const afterScroll = await drawer.evaluate((el) => {
    const body = el.querySelector('.glance-drawer-body');
    return {
      drawerScrollTop: el.scrollTop,
      bodyScrollTop: body.scrollTop,
    };
  });
  expect(afterScroll.drawerScrollTop).toBeGreaterThan(200);
  expect(afterScroll.bodyScrollTop).toBe(0);

  const closeAfter = await close.boundingBox();
  const titleAfter = await page.locator('#glanceDrawerTitle').boundingBox();
  expect(closeAfter).not.toBeNull();
  expect(Math.abs(closeAfter.y - closeBefore.y)).toBeLessThan(2);
  expect(closeAfter.y).toBeGreaterThanOrEqual(box.y - 1);
  expect(closeAfter.y).toBeLessThan(box.y + 40);
  expect(titleAfter).not.toBeNull();
  expect(titleAfter.y).toBeLessThan(closeAfter.y);
});
