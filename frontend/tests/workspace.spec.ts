import { expect, test, type Page } from '@playwright/test'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'

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

test('缺少归因字段时保留之前已核实的地区证据', async ({ page }) => {
  await page.goto('/')
  await expect(page.locator('#dataset')).toContainText('合成数据 · 96 单')
  await ask(page, '是哪家快递的问题？')
  const current = page.getByRole('article').first()
  await expect(current).toContainText('数据能力缺失')
  await expect(current).toContainText('承运商标识和物流轨迹')
  await ask(page, '2018年7月和8月按州比较超期单数增量')
  await expect(current.getByText('州拆分查询', { exact: true })).toBeVisible()
  await ask(page, '是哪家快递的问题？')
  await expect(current).toContainText('部分结果')
  await current.getByText('已核实的地区证据', { exact: true }).click()
  await expect(current.getByRole('table')).toContainText('圣保罗州')
})

test('HTTP 网关超时后保留可展开的历史证据', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByText('规则调查', { exact: true })).toBeVisible()
  await ask(page, '2018年8月超期率是多少？')
  const previous = page.getByRole('article').first()
  await expect(previous).toContainText('8 单超期 / 24 个有效样本')
  await page.route('**/api/analysis', (route) =>
    route.fulfill({ status: 504, contentType: 'text/html', body: 'Gateway Timeout' }),
  )
  await ask(page, '2018年8月配送阶段耗时')
  await expect(page.getByRole('alert')).toHaveText('分析请求超时，请重试。')
  await previous.getByText('指标查询', { exact: true }).click()
  await expect(previous.getByRole('table')).toContainText('8/24')
})

test('数据库连接中断时显示失败，恢复后同一会话可查', async ({ page }) => {
  const project = process.env.PLAYWRIGHT_FAULT_COMPOSE_PROJECT
  test.skip(!project, '需要显式指定用于暂停数据库的独立 Compose 项目')
  test.setTimeout(45_000)
  async function database(action: 'pause' | 'unpause') {
    await promisify(execFile)(
      'docker',
      ['compose', '--env-file', '.env.example', '-p', project!, action, 'db'],
      { cwd: new URL('../..', import.meta.url) },
    )
  }

  await page.goto('/')
  await expect(page.getByText('规则调查', { exact: true })).toBeVisible()
  await ask(page, '2018年8月超期率是多少？')
  const cards = page.getByRole('article')
  await expect(cards.first()).toContainText('8 单超期 / 24 个有效样本')
  await database('pause')
  try {
    await ask(page, '2018年8月配送阶段耗时')
    await expect(cards.first()).toContainText('执行失败', { timeout: 15_000 })
    await expect(cards.first()).toContainText('数据库暂不可用')
    await cards.nth(1).getByText('指标查询', { exact: true }).click()
    await expect(cards.nth(1).getByRole('table')).toContainText('8/24')
  } finally {
    await database('unpause')
  }
  await ask(page, '2018年8月已送达订单数')
  await expect(cards.first()).toContainText('24 单')
  await expect(cards).toHaveCount(3)
})

test('固定模式在无密钥整栈中完成地区调查', async ({ page }) => {
  test.skip(process.env.PLAYWRIGHT_EXPECTED_MODE !== 'fixed', '需要使用固定模型重放模式')
  await page.goto('/')
  await expect(page.getByText('固定模型重放', { exact: true })).toBeVisible()
  await expect(page.locator('#dataset')).toContainText('合成数据 · 96 单')
  await page.getByRole('button', { name: '排查超期集中地区' }).click()
  await page.getByRole('button', { name: '开始分析' }).click()
  const current = page.getByRole('article').first()
  await expect(current).toContainText('已完成')
  await current.getByText('城市拆分查询', { exact: true }).click()
  await expect(current.getByRole('table')).toContainText('sao paulo')
  await expect(current.getByRole('table')).toContainText('+5 单')
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
