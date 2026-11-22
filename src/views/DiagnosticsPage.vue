<template>
  <section class="mx-auto w-full max-w-7xl space-y-5 p-4 pb-24 lg:pb-4">
    <header class="flex flex-wrap items-center justify-between gap-3">
      <h1 class="text-xl font-semibold">{{ t('diagnostics') }}</h1>
      <button
        v-if="localBackend"
        class="btn btn-sm"
        :disabled="loading"
        @click="refresh"
      >
        {{ t('refresh') }}
      </button>
    </header>

    <p
      v-if="!localBackend"
      role="status"
    >
      {{ t('diagnosticBackend') }}
    </p>
    <div v-else>
      <p
        v-if="error"
        class="alert alert-error mb-4"
        role="alert"
      >
        {{ t('diagnosticUnavailable') }}
      </p>
      <p
        v-else-if="loading && !snapshot"
        role="status"
      >
        {{ t('diagnosticLoading') }}
      </p>

      <section class="mb-5 space-y-2">
        <h2 class="font-semibold">{{ t('diagnosticRoutes') }}</h2>
        <ul
          v-if="groups.length"
          class="grid grid-cols-2 gap-2 lg:grid-cols-3"
        >
          <li
            v-for="[name, node] in groups"
            :key="name"
            class="rounded-box bg-base-200 p-3"
          >
            <div class="text-sm">{{ name }}</div>
            <strong class="break-words">{{ node }}</strong>
          </li>
        </ul>
        <p v-else>{{ t('noData') }}</p>
      </section>

      <template v-if="snapshot">
        <section class="mb-5 space-y-2">
          <h2 class="font-semibold">{{ t('diagnosticScores') }}</h2>
          <div class="flex flex-wrap gap-x-4 gap-y-1 text-sm">
            <p>
              {{ t('diagnosticScores') }} ·
              {{ sampleTime(snapshot.scoresAt, snapshot.scoresMaxAge) }}
            </p>
            <p>
              {{ t('diagnosticPurity') }} ·
              {{ sampleTime(snapshot.purityAt, snapshot.purityMaxAge) }}
            </p>
            <p v-if="snapshot.refresh">
              {{ t('refresh') }} · {{ statusOf(snapshot.refresh.status) }} ·
              {{ date(snapshot.refresh.at) }}
            </p>
          </div>
          <div
            v-if="snapshot.nodes.length"
            class="rounded-box bg-base-200 overflow-x-auto"
            tabindex="0"
            :aria-label="t('diagnosticScores')"
          >
            <table class="table-sm table">
              <thead>
                <tr>
                  <th scope="col">{{ t('diagnosticNode') }}</th>
                  <th scope="col">{{ t('diagnosticScores') }}</th>
                  <th scope="col">{{ t('diagnosticPurity') }}</th>
                  <th scope="col">{{ t('diagnosticRegion') }}</th>
                  <th
                    v-for="platform in platforms"
                    :key="platform"
                    scope="col"
                  >
                    {{ platform }}
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="node in snapshot.nodes"
                  :key="node.name"
                >
                  <th scope="row">{{ node.name }}</th>
                  <td>
                    {{ node.score ?? '—' }}
                    <div
                      v-if="node.base != null"
                      class="text-xs opacity-70"
                    >
                      {{ node.base }}
                    </div>
                  </td>
                  <td>
                    {{ node.purity ?? '—' }}
                    <div
                      v-if="node.ip"
                      class="text-xs opacity-70"
                    >
                      {{ node.ip }}
                    </div>
                  </td>
                  <td>
                    {{ [node.region, node.country].filter(Boolean).join(' → ') || '—' }}
                    <div
                      v-if="node.regionMatch != null"
                      class="text-xs opacity-70"
                    >
                      {{
                        t(node.regionMatch ? 'diagnosticRegionMatch' : 'diagnosticRegionMismatch')
                      }}
                    </div>
                  </td>
                  <td
                    v-for="platform in platforms"
                    :key="platform"
                  >
                    <template v-if="node.platforms[platform]">
                      <div>{{ node.platforms[platform]?.score ?? '—' }}</div>
                      <div class="text-xs whitespace-nowrap opacity-70">
                        {{ platformDetails(node.platforms[platform]!) }}
                      </div>
                    </template>
                    <template v-else>—</template>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-else>{{ t('diagnosticMissing') }}</p>
        </section>

        <section class="space-y-2">
          <h2 class="font-semibold">{{ t('diagnosticWatchdog') }}</h2>
          <ul
            v-if="snapshot.events.length"
            class="divide-base-300 rounded-box bg-base-200 divide-y px-3"
          >
            <li
              v-for="(event, index) in snapshot.events"
              :key="index"
              class="flex flex-wrap gap-x-3 gap-y-1 py-2 text-sm"
            >
              <time :datetime="event.at || undefined">{{ date(event.at) }}</time>
              <strong>{{ event.group || t('diagnosticSummary') }}</strong>
              <span>{{ statusOf(event.status) }}</span>
              <span v-if="event.node">{{ event.node }}</span>
              <span v-if="event.action">{{ t(actionLabels[event.action] || 'statusLabel') }}</span>
              <span v-if="event.selected">→ {{ event.selected }}</span>
              <span v-if="event.targets != null">{{ event.ok ?? 0 }}/{{ event.targets }}</span>
              <span v-if="event.groups != null"
                >{{ t('diagnosticProblems') }} {{ event.problems ?? 0 }}/{{ event.groups }}</span
              >
            </li>
          </ul>
          <p v-else>{{ t('diagnosticMissing') }}</p>
        </section>
      </template>
      <p
        v-else-if="!error && !loading"
        role="status"
      >
        {{ t('diagnosticMissing') }}
      </p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { getUrlFromBackend } from '@/helper/utils'
import { activeBackend } from '@/store/setup'
import { useIntervalFn } from '@vueuse/core'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

type Platform = {
  score: number | null
  availability: number | null
  median_ms: number | null
  jitter_ms: number | null
}

type Snapshot = {
  version: 1
  scoresAt: string | null
  purityAt: string | null
  scoresMaxAge: number
  purityMaxAge: number
  nodes: {
    name: string
    score: number | null
    base: number | null
    ip: string | null
    purity: number | null
    country: string
    region: string
    regionMatch: boolean | null
    platforms: Record<string, Platform>
  }[]
  events: {
    at: string | null
    status: string
    action: string | null
    group: string
    node: string
    selected: string
    groups?: number
    problems?: number
    targets?: number
    ok?: number
  }[]
  refresh: { status: string; at: string } | null
}

const { t, locale } = useI18n()
const api = new URL('./mihomo-api/', window.location.href)
const snapshot = ref<Snapshot | null>(null)
const groups = ref<[string, string][]>([])
const error = ref(false)
const loading = ref(false)
let pending: AbortController | undefined

const localBackend = computed(() => {
  const backend = activeBackend.value
  if (backend?.type !== 'clash') return undefined
  try {
    const address = new URL(getUrlFromBackend(backend))
    return address.origin === api.origin && address.pathname.replace(/\/?$/, '/') === api.pathname
      ? backend
      : undefined
  } catch {
    return undefined
  }
})

const platforms = computed(() => [
  ...new Set(snapshot.value?.nodes.flatMap((n) => Object.keys(n.platforms))),
])
const date = (value: string | null) =>
  value ? new Date(value).toLocaleString(locale.value) : t('diagnosticMissing')
const sampleTime = (value: string | null, maxAge: number) =>
  value && Date.now() - Date.parse(value) > maxAge * 1000
    ? `${date(value)} · ${t('diagnosticStale')}`
    : date(value)
const platformDetails = (metric: Platform) =>
  [
    metric.availability == null ? '' : `${Math.round(metric.availability * 100)}%`,
    metric.median_ms == null ? '' : `${metric.median_ms} ms`,
    metric.jitter_ms == null ? '' : `±${metric.jitter_ms} ms`,
  ]
    .filter(Boolean)
    .join(' · ')

const statusLabels: Record<string, string> = {
  ok: 'diagnosticHealthy',
  healthy: 'diagnosticHealthy',
  degraded: 'diagnosticDegraded',
  'group-unreadable': 'diagnosticUnavailable',
  failed: 'diagnosticUnavailable',
  summary: 'diagnosticSummary',
}
const statusOf = (status: string) => t(statusLabels[status] || 'statusLabel')
const actionLabels: Record<string, string> = {
  switched: 'diagnosticSwitched',
  'selection-changed': 'diagnosticSelectionChanged',
  'reselect-unconfirmed': 'diagnosticUnconfirmed',
  'no-healthy-alternative': 'diagnosticNoAlternative',
}

async function refresh() {
  const backend = localBackend.value
  if (!backend || pending) return
  const controller = new AbortController()
  pending = controller
  const timer = window.setTimeout(() => controller.abort(), 8000)
  loading.value = true
  error.value = false
  const options: RequestInit = {
    headers: { Authorization: `Bearer ${backend.password}` },
    cache: 'no-store',
    redirect: 'error',
    signal: controller.signal,
  }
  try {
    const [proxyResponse, stateResponse] = await Promise.all([
      fetch(new URL('proxies', api), options),
      fetch(new URL('./autofallback/state.json', window.location.href), options),
    ])
    if (!proxyResponse.ok) throw new Error('Mihomo unavailable')
    const body = (await proxyResponse.json()) as { proxies: Record<string, { now?: string }> }
    const selected = Object.entries(body.proxies).flatMap(([name, group]): [string, string][] =>
      name !== 'GLOBAL' && group.now ? [[name, group.now]] : [],
    )
    let state: Snapshot | null = null
    if (stateResponse.status !== 404) {
      if (!stateResponse.ok) throw new Error('Snapshot unavailable')
      state = await stateResponse.json()
      if (state?.version !== 1) throw new Error('Unsupported snapshot')
    }
    if (!controller.signal.aborted && pending === controller) {
      groups.value = selected
      snapshot.value = state
    }
  } catch {
    if (pending === controller) error.value = true
  } finally {
    window.clearTimeout(timer)
    if (pending === controller) {
      pending = undefined
      loading.value = false
    }
  }
}

watch(
  localBackend,
  () => {
    pending?.abort()
    pending = undefined
    loading.value = false
    snapshot.value = null
    groups.value = []
    error.value = false
    void refresh()
  },
  { immediate: true },
)
useIntervalFn(refresh, 30000)
onBeforeUnmount(() => {
  pending?.abort()
  pending = undefined
})
</script>
