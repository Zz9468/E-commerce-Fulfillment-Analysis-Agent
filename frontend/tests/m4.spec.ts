import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const mode = process.env.M4_WORKSPACE_MODE
const recording = process.env.M4_DEMO_VIDEO
const cases = new Map<string, string>(
  readFileSync(new URL('../../eval/cases/holdout.jsonl', import.meta.url), 'utf8')
    .trim()
    .split('\n')
    .map((line) => {
      const { id, question } = JSON.parse(line)
      return [id, question]
    }),
)

test('M4 从留出题原问进入 Olist 工作台并核对证据和权限', async ({ browser, baseURL }) => {
  test.skip(!mode, '需要 Olist 数据和明确的 M4_WORKSPACE_MODE')
  test.setTimeout(245_000)
  const context = await browser.newContext({
    baseURL,
    viewport: { width: 1440, height: 1000 },
    recordVideo: recording
      ? {
          dir: fileURLToPath(new URL('../test-results/m4/', import.meta.url)),
          size: { width: 1440, height: 1000 },
        }
      : undefined,
  })
  const page = await context.newPage()
  try {
    const cards = page.getByRole('article')
    const answer = cards.first()
    const dataset = page.getByLabel('数据版本')
    const modeLabel = mode === 'model' ? '模型调查' : '规则调查'
    async function ask(question: string, count: number) {
      await page.getByLabel('分析问题').fill(question)
      await page.getByRole('button', { name: '开始分析' }).click()
      await expect(cards).toHaveCount(count, { timeout: 225_000 })
    }
    async function pause() {
      if (recording) await page.waitForTimeout(2200)
    }

    await page.goto('/')
    await expect(page.getByText(modeLabel, { exact: true })).toBeVisible()
    await expect(dataset.locator('option:checked')).toContainText('Olist')
    await ask(cases.get('H01')!, 1)
    await expect(answer).toContainText('393 单超期 / 6,351 个有效样本')
    await answer.getByText('指标查询', { exact: true }).click()
    await expect(answer.getByRole('table')).toContainText('393/6351')
    await pause()

    await ask(cases.get('H21')!, 2)
    await expect(answer).toContainText('+2.81 百分点')
    await answer.getByText('州拆分查询', { exact: true }).click()
    await expect(answer.getByRole('table')).toContainText('185 单')
    await pause()
    await answer.getByRole('button', { name: '查看 SP 城市' }).click()
    await expect(cards).toHaveCount(3)
    await expect(answer).toContainText('2018-07 → 2018-08')
    await answer.getByText('城市拆分查询', { exact: true }).click()
    await expect(answer.getByRole('table')).toContainText('sao paulo')
    await expect(answer.getByRole('table')).toContainText('73 单')
    await pause()

    await ask('是哪家快递的问题？', 4)
    await expect(answer).toContainText('承运商标识和物流轨迹')
    await answer.getByText('已核实的地区证据', { exact: true }).click()
    await expect(answer.getByRole('table')).toContainText('73 单')
    await pause()

    await page.getByLabel('演示账号').selectOption('demo-sp')
    await expect(page.getByText('可查范围 · 圣保罗州 · SP')).toBeVisible()
    await ask(cases.get('H36')!, 1)
    await expect(answer).toContainText('权限受限')
    await expect(answer.getByText('查询证据', { exact: true })).toHaveCount(0)
    await pause()
    await ask(cases.get('H02')!, 2)
    await expect(answer).toContainText('100 单超期 / 2,712 个有效样本')
    await pause()
  } finally {
    await context.close()
  }
  if (recording) {
    await page
      .video()!
      .saveAs(fileURLToPath(new URL('../../eval/demo/workspace.webm', import.meta.url)))
  }
})
