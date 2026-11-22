<template>
  <main class="mx-auto w-full max-w-2xl overflow-auto p-5">
    <RouterLink
      :to="{ name: activeBackend ? ROUTE_NAME.settings : ROUTE_NAME.setup }"
      class="btn btn-ghost btn-sm mb-3"
    >{{ t('back') }}</RouterLink>
    <h1 class="text-xl font-semibold">{{ t('managedCore') }}</h1>

    <form v-if="!state" class="mt-4 flex flex-wrap items-end gap-2" @submit.prevent="refresh">
      <label class="form-control">
        <span class="mb-1 text-sm">{{ t('password') }}</span>
        <input
          v-model="credential"
          type="password"
          autocomplete="current-password"
          class="input input-bordered"
          required
        />
      </label>
      <button class="btn btn-primary" :disabled="waiting">{{ t('submit') }}</button>
    </form>

    <template v-if="state">
      <dl class="mt-4 grid grid-cols-[auto_1fr] gap-x-5 gap-y-1 text-sm">
        <dt>{{ t('managedCoreStatus') }}</dt>
        <dd>{{ t('managedCoreStatus_' + state.status) }}</dd>
        <dt>{{ t('managedCoreVersion') }}</dt>
        <dd>{{ state.current || '—' }}</dd>
        <dt>{{ t('managedCorePrevious') }}</dt>
        <dd>{{ state.previous || '—' }}</dd>
      </dl>
      <div class="my-4 flex flex-wrap gap-2" :aria-busy="!!job">
        <button class="btn btn-sm" :disabled="!!job || waiting || state.status === 'running'"
          @click="run('start')">{{ t('managedCoreStart') }}</button>
        <button class="btn btn-sm" :disabled="!!job || waiting || state.status !== 'running'"
          @click="run('stop')">{{ t('managedCoreStop') }}</button>
        <button class="btn btn-sm" :disabled="!!job || waiting || state.status !== 'running'"
          @click="run('restart')">{{ t('restartCore') }}</button>
        <button class="btn btn-sm" :disabled="!!job || waiting || !state.previous"
          @click="run('rollback')">{{ t('managedCoreRollback') }}</button>
      </div>
      <form class="flex flex-wrap items-end gap-2" @submit.prevent="run('upgrade')">
        <label class="form-control">
          <span class="mb-1 text-sm">{{ t('managedCoreRelease') }}</span>
          <input v-model.trim="version" class="input input-bordered" placeholder="v1.19.33"
            pattern="v[0-9]+\.[0-9]+\.[0-9]+" required />
        </label>
        <button class="btn btn-primary" :disabled="!!job || waiting">{{ t('upgradeCore') }}</button>
      </form>

      <section v-if="subscriptionPool" class="mt-6 space-y-3">
        <h2 class="font-semibold">{{ t('managedSubscriptions') }}</h2>
        <ul v-if="subscriptionPool.subscriptions.length" class="divide-y divide-base-300">
          <li v-for="source in subscriptionPool.subscriptions" :key="source.name"
            class="flex flex-wrap items-center justify-between gap-2 py-2">
            <div class="min-w-0">
              <div class="break-all font-medium">{{ source.name }}</div>
              <div class="break-all text-xs opacity-70">
                {{ source.host }} · {{ t(source.enabled ? 'managedSubscriptionEnabled' : 'managedSubscriptionDisabled') }}
                <span v-if="source.cached"> · {{ t('managedSubscriptionCached') }}</span>
              </div>
            </div>
            <div class="flex gap-2">
              <button class="btn btn-sm" :disabled="!!job || waiting || state.busy"
                @click="updateSubscription({ action: source.enabled ? 'disable' : 'enable', name: source.name })">
                {{ t(source.enabled ? 'managedSubscriptionDisable' : 'managedSubscriptionEnable') }}
              </button>
              <button class="btn btn-sm btn-ghost text-error" :disabled="!!job || waiting || state.busy"
                @click="updateSubscription({ action: 'delete', name: source.name })">{{ t('delete') }}</button>
            </div>
          </li>
        </ul>
        <p v-else class="text-sm opacity-70">{{ t('noData') }}</p>
        <form class="flex flex-wrap items-end gap-2" @submit.prevent="addSubscription">
          <label class="form-control">
            <span class="mb-1 text-sm">{{ t('name') }}</span>
            <input v-model.trim="subscriptionName" class="input input-bordered"
              pattern="[a-z0-9][a-z0-9_\x2d]{0,31}" maxlength="32" required />
          </label>
          <label class="form-control min-w-0 flex-1">
            <span class="mb-1 text-sm">{{ t('managedSubscriptionUrl') }}</span>
            <input v-model.trim="subscriptionUrl" class="input input-bordered w-full" type="url"
              autocomplete="off" maxlength="2048" required />
          </label>
          <label class="flex items-center gap-2 text-sm">
            <input v-model="subscriptionEnabled" type="checkbox" class="checkbox checkbox-sm" />
            {{ t('managedSubscriptionEnable') }}
          </label>
          <button class="btn btn-primary" :disabled="!!job || waiting || state.busy">
            {{ t('managedSubscriptionAdd') }}
          </button>
        </form>
        <div v-if="subscriptionPool.pending" class="flex flex-wrap items-center gap-2 text-sm">
          <p role="status">{{ t('managedSubscriptionPending') }}</p>
          <button v-if="state.status === 'running'" class="btn btn-sm"
            :disabled="!!job || waiting || state.busy" @click="updateSubscription({ action: 'apply' })">
            {{ t('managedSubscriptionApply') }}
          </button>
        </div>
      </section>

      <section class="mt-6 space-y-2">
        <h2 class="font-semibold">{{ t('managedRules') }}</h2>
        <p class="text-sm">{{ t('managedRulesCurrent') }} {{ ruleSource || t('managedRulesPreset') }}</p>
        <form class="flex flex-wrap items-end gap-2" @submit.prevent="run('rules')">
          <label class="form-control min-w-0 flex-1">
            <span class="mb-1 text-sm">{{ t('managedRulesUrl') }}</span>
            <input v-model.trim="rulesUrl" class="input input-bordered w-full" type="url"
              placeholder="https://…" maxlength="1024" required />
          </label>
          <button class="btn btn-primary" :disabled="!!job || waiting || state.status !== 'running'">
            {{ t('managedRulesUpdate') }}
          </button>
        </form>
        <p v-if="rulesUpdated" class="text-sm text-success" role="status">{{ t('managedRulesSaved') }}</p>
      </section>
    </template>

    <p v-if="job" class="mt-4 text-sm" role="status">{{ t('managedCoreWorking') }}</p>
    <p v-if="error || state?.message" class="alert alert-error mt-4" role="alert">
      {{ error || state?.message }}
    </p>
  </main>
</template>

<script setup lang="ts">
import { ROUTE_NAME } from '@/constant'
import { isManagedBackend, managedCoreUrl } from '@/helper/managed-core'
import { activeBackend, backendList } from '@/store/setup'
import { useIntervalFn } from '@vueuse/core'
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'

type Action = 'start' | 'stop' | 'restart' | 'upgrade' | 'rollback' | 'rules'
type SubscriptionMutation =
  | { action: 'add'; name: string; url: string; enabled: boolean }
  | { action: 'delete' | 'enable' | 'disable'; name: string }
  | { action: 'apply' }
type SubscriptionPool = {
  subscriptions: { name: string; enabled: boolean; host: string; cached: boolean }[]
  pending: boolean
}
type CoreState = {
  status: 'running' | 'stopped' | 'starting' | 'stopping' | 'failed'
  current: string | null
  previous: string | null
  message: string
  busy: boolean
  job?: { action: Action | 'refresh'; status: 'running' | 'ok' | 'failed'; message: string }
}

const { t } = useI18n()
const credential = ref(backendList.value.find(isManagedBackend)?.password || '')
const state = ref<CoreState>()
const version = ref('')
const rulesUrl = ref('')
const ruleSource = ref('')
const rulesUpdated = ref(false)
const managedRulesUrl = new URL('./autofallback/rules', window.location.href)
const subscriptionsUrl = new URL('./autofallback/subscriptions', window.location.href)
const subscriptionPool = ref<SubscriptionPool>()
const subscriptionName = ref('')
const subscriptionUrl = ref('')
const subscriptionEnabled = ref(true)
const job = ref('')
const waiting = ref(false)
const error = ref('')

async function request(url: URL, body?: { action: Action; version?: string; url?: string } | SubscriptionMutation) {
  const response = await fetch(url, {
    method: body ? 'POST' : 'GET',
    headers: {
      Authorization: `Bearer ${credential.value}`,
      ...(body ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
    cache: 'no-store',
    redirect: 'error',
    signal: AbortSignal.timeout(5000),
  })
  if (response.status === 401) {
    state.value = undefined
    subscriptionPool.value = undefined
    job.value = ''
    throw new Error(t('unauthorizedTip'))
  }
  if (response.status === 404 && job.value) {
    job.value = ''
    throw new Error(t('managedCoreUnavailable'))
  }
  const data = await response.json()
  if (!response.ok) throw new Error(data.error || t('managedCoreUnavailable'))
  return data
}

async function refresh() {
  if (waiting.value || !credential.value) return
  waiting.value = true
  if (!state.value) error.value = ''
  try {
    const url = new URL(managedCoreUrl)
    if (job.value) url.searchParams.set('job', job.value)
    const first = !state.value
    const next: CoreState = await request(url)
    state.value = next
    if (next.job && next.job.status !== 'running') {
      error.value = next.job.message
      rulesUpdated.value = next.job.action === 'rules' && next.job.status === 'ok'
      job.value = ''
    }
    if (first || rulesUpdated.value && next.job?.action === 'rules') {
      const { source } = await request(managedRulesUrl)
      ruleSource.value = source || ''
      rulesUrl.value = ruleSource.value
    }
    if (!subscriptionPool.value || subscriptionPool.value.pending || next.job?.action === 'refresh') {
      subscriptionPool.value = await request(subscriptionsUrl)
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : t('managedCoreUnavailable')
  } finally {
    waiting.value = false
  }
}

async function run(action: Action) {
  if (waiting.value || job.value) return
  if (action !== 'start' && action !== 'rules' && !window.confirm(t('managedCoreConfirm'))) return
  error.value = ''
  rulesUpdated.value = false
  waiting.value = true
  try {
    const data = await request(managedCoreUrl, {
      action,
      ...(action === 'upgrade' ? { version: version.value } : {}),
      ...(action === 'rules' ? { url: rulesUrl.value } : {}),
    })
    job.value = data.job
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : t('managedCoreUnavailable')
  } finally {
    waiting.value = false
  }
  void refresh()
}

async function updateSubscription(body: SubscriptionMutation) {
  if (waiting.value || job.value) return
  if (body.action === 'delete' && !window.confirm(t('managedSubscriptionConfirmDelete'))) return
  error.value = ''
  waiting.value = true
  try {
    const data = await request(subscriptionsUrl, body)
    job.value = data.job || ''
    if (body.action === 'add') {
      subscriptionName.value = ''
      subscriptionUrl.value = ''
      subscriptionEnabled.value = true
    }
    subscriptionPool.value = await request(subscriptionsUrl)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : t('managedCoreUnavailable')
  } finally {
    waiting.value = false
  }
  void refresh()
}

function addSubscription() {
  void updateSubscription({
    action: 'add', name: subscriptionName.value, url: subscriptionUrl.value,
    enabled: subscriptionEnabled.value,
  })
}

onMounted(refresh)
useIntervalFn(() => { if (state.value || job.value) void refresh() }, 1500)
</script>
