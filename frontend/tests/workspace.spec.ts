import { expect, test, type Page } from '@playwright/test'

async function ask(page: Page, question: string) {
  await page.getByLabel('分析问题').fill(question)
  await page.getByRole('button', { name: '开始分析' }).click()
}

test('查询数字、展开真实证据、从旧证据下钻', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByText('规则调查', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '查一个数字' }).click()
  await page.getByRole('button', { name: '开始分析' }).click()
  await expect(page.getByRole('article').first()).toContainText('8 单超期 / 24 个有效样本')
  await page.getByText('指标查询', { exact: true }).click()
  await expect(page.getByRole('table').first()).toContainText('8/24')

  await ask(page, '2018年7月和8月按州比较超期单数增量')
  await page.getByText('州拆分查询', { exact: true }).click()
  await expect(page.getByRole('button', { name: '查看 SP 城市' })).toBeVisible()
  await ask(page, '2018年9月平均送达耗时是多少？')
  await expect(page.getByRole('article').first()).toContainText('2018-09')
  await page.getByRole('button', { name: '查看 SP 城市' }).click()
  const result = page.getByRole('article').first()
  await expect(result).toContainText('2018-07 → 2018-08')
  await result.getByText('城市拆分查询', { exact: true }).click()
  await expect(result.getByRole('table')).toContainText('sao paulo')
  await expect(result.getByRole('table')).toContainText('+5 单')

  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390)
})

test('澄清与空样本时显示对应状态', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByText('规则调查', { exact: true })).toBeVisible()
  await page.getByLabel('年份').selectOption({ label: '待确认' })
  await ask(page, '8月配送变差了吗？')
  await expect(page.getByText('待补充条件', { exact: true })).toBeVisible()
  await expect(page.getByText('查询证据', { exact: true })).toHaveCount(0)
  await ask(page, '2018年')
  await expect(page.getByRole('article').first()).toContainText('25.00 百分点')
  await ask(page, '2018年11月超期率是多少？')
  await expect(
    page.getByRole('article').first().getByText('无可计算数据', { exact: true }),
  ).toBeVisible()
})

for (const reset of ['账号', '会话'] as const) {
  test(`切换${reset}后不显示迟到结果`, async ({ page }) => {
    await page.goto('/')
    await expect(page.getByText('规则调查', { exact: true })).toBeVisible()
    let release = () => {}
    const hold = new Promise<void>((resolve) => {
      release = resolve
    })
    let waiting = () => {}
    const intercepted = new Promise<void>((resolve) => {
      waiting = resolve
    })
    const routeFinished = page.route('**/api/analysis', async (route) => {
      if (route.request().postDataJSON().question !== '2018年8月全国超期率') {
        await route.continue()
        return
      }
      const response = await route.fetch()
      waiting()
      await hold
      await route.fulfill({ response }).catch(() => undefined)
    })
    await routeFinished
    await ask(page, '2018年8月全国超期率')
    await intercepted
    if (reset === '账号') {
      await page.getByLabel('演示账号').selectOption('demo-sp')
      await expect(page.getByText('可查范围 · 圣保罗州 · SP')).toBeVisible()
    } else {
      await page.getByRole('button', { name: '新会话' }).click()
    }
    release()
    await ask(page, '2018年8月已送达订单数')
    const cards = page.getByRole('article')
    await expect(cards).toHaveCount(1)
    await expect(cards.first()).toContainText(reset === '账号' ? '12 单' : '24 单')
    if (reset === '账号') {
      await ask(page, '2018年8月全国超期率，请比较')
      await expect(page.getByText('权限受限', { exact: true })).toBeVisible()
    }
  })
}
