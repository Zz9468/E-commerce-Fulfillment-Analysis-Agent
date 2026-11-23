import vue from 'eslint-plugin-vue'
import { defineConfigWithVueTs, vueTsConfigs } from '@vue/eslint-config-typescript'
import prettier from 'eslint-config-prettier/flat'

export default defineConfigWithVueTs(
  { ignores: ['dist/**', 'test-results/**', 'playwright-report/**'] },
  vue.configs['flat/recommended'],
  vueTsConfigs.recommended,
  prettier,
)
