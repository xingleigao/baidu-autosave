<template>
  <el-card v-loading="loading">
    <h3>轻量微信网关</h3>
    <el-alert
      title="多人共用：每人分别扫码绑定；所有已绑定微信均可管理同一套网盘账号、任务和参数。请只添加可信用户。"
      type="warning" :closable="false" show-icon
    />
    <el-alert
      title="ClawBot 的回复有会话时限和额度。微信公众号通知继续保留在“通知设置”中，建议用于定时推送；两者互不替代。"
      type="warning" :closable="false" show-icon class="gateway-space"
    />
    <el-alert v-if="error" :title="error" type="error" :closable="false" class="gateway-space" />
    <el-form label-width="150px" class="gateway-space">
      <el-form-item label="启用微信网关">
        <el-switch v-model="gateway.enabled" :disabled="busy || !gateway.available" @change="saveSettings" />
      </el-form-item>
      <el-form-item label="网关推送任务通知">
        <el-switch v-model="gateway.notifications" :disabled="busy" @change="saveSettings" />
        <span class="gateway-tip">默认关闭，不影响原公众号、Push Plus 等通知</span>
      </el-form-item>
      <el-form-item label="当前管理账号">
        <el-select v-model="selectedAccount" placeholder="请先添加账号">
          <el-option v-for="account in gateway.accounts" :key="account.id" :label="account.name" :value="account.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="登录状态">
        <el-tag :type="currentAccount?.connected ? 'success' : 'info'">
          {{ currentAccount?.connected ? '已保存登录会话' : '未登录 / 会话失效' }}
        </el-tag>
      </el-form-item>
      <el-form-item v-if="currentAccount?.owner" label="绑定微信标识">
        <span class="gateway-id">{{ currentAccount.owner }}</span>
      </el-form-item>
      <el-form-item v-if="currentAccount?.bot_id" label="机器人标识">
        <span class="gateway-id">{{ currentAccount.bot_id }}</span>
      </el-form-item>
      <el-form-item>
        <div class="gateway-actions">
          <el-button type="primary" :disabled="busy || !gateway.available || !currentAccount || currentAccount.connected || loginPending" @click="startLogin">
            获取登录二维码
          </el-button>
          <el-button :disabled="busy || !currentAccount?.connected || !gateway.enabled" @click="testMessage">发送测试消息</el-button>
          <el-button type="danger" plain :disabled="busy || !gateway.available || !currentAccount" @click="logout">退出此账号</el-button>
        </div>
      </el-form-item>
    </el-form>
    <div class="gateway-actions gateway-space">
      <el-input v-model="accountName" maxlength="40" placeholder="新增账号名称，如：家人 A" class="gateway-app-input" />
      <el-button type="primary" :disabled="busy || !gateway.available || !accountName.trim()" @click="createAccount">添加微信账号</el-button>
    </div>
    <el-table :data="gateway.accounts" class="gateway-space">
      <el-table-column prop="name" label="共用账号" min-width="110" />
      <el-table-column label="状态" min-width="100">
        <template #default="scope">{{ scope.row.connected ? '已登录' : '未登录 / 失效' }}{{ scope.row.is_default ? '（默认）' : '' }}</template>
      </el-table-column>
      <el-table-column prop="last_error" label="最近错误" min-width="160" />
      <el-table-column label="操作" min-width="180">
        <template #default="scope">
          <el-button text :disabled="busy || scope.row.is_default" @click="setDefaultAccount(scope.row.id)">设为默认</el-button>
          <el-button text type="danger" :disabled="busy" @click="removeAccount(scope.row)">移除</el-button>
        </template>
      </el-table-column>
    </el-table>
    <p class="gateway-tip">最多 10 个账号，每次只进行一个扫码流程。登录二维码对应：{{ loginAccountName }}。开启网关任务推送后，会分别尝试通知所有已登录账号。</p>
    <div v-if="gateway.login.qr_image" class="gateway-qr">
      <img :src="gateway.login.qr_image" width="240" height="240" alt="微信登录二维码，请使用微信扫码" />
    </div>
    <p v-if="gateway.login.message">{{ gateway.login.message }}</p>
    <div v-if="gateway.login.status === 'need_verifycode'" class="gateway-actions gateway-space">
      <el-input v-model="verifyCode" type="password" show-password maxlength="16" inputmode="numeric" placeholder="输入手机微信显示的数字配对码" class="gateway-app-input" />
      <el-button type="primary" :disabled="busy || !verifyCode" @click="submitVerifyCode">提交配对码</el-button>
    </div>
    <el-alert v-if="gateway.last_error" :title="gateway.last_error" type="warning" :closable="false" />
    <el-alert v-if="currentAccount?.last_error" :title="currentAccount.last_error" type="warning" :closable="false" />
    <p class="gateway-tip">扫码确认后自动启用。先在微信给机器人发送“帮助”，再测试发送；网页显示登录不代表微信服务实时在线。</p>
    <p class="gateway-tip">支持：帮助、搜索 影片名、追剧 ID [更新频率]（如：追剧 118943 每周六周日6-8点，或每天6-8点每30分钟）、任务、参数、设置 save_dir /追剧、检查更新。也可加“网盘 ”前缀。追剧会分阶段回复，最终只显示链接、更新频率和保存目录。</p>
    <el-divider />
    <h3>其他 Docker / 应用共用</h3>
    <p class="gateway-tip">每个应用的独立令牌固定绑定一个微信账号，只能向该账号发送文本，不能执行网盘命令。每个应用每分钟最多尝试发送 10 次。</p>
    <div class="gateway-actions gateway-space">
      <el-input v-model="applicationName" maxlength="40" placeholder="应用名称，如：农场通知" class="gateway-app-input" />
      <el-button type="primary" :disabled="busy || !applicationName.trim() || !selectedAccount" @click="createApplication">为当前账号创建令牌</el-button>
    </div>
    <el-table :data="gateway.apps" class="gateway-space">
      <el-table-column prop="name" label="应用名称" />
      <el-table-column label="接收账号">
        <template #default="scope">{{ gateway.accounts.find(account => account.id === scope.row.account_id)?.name || '账号不存在' }}</template>
      </el-table-column>
      <el-table-column label="操作" width="100">
        <template #default="scope">
          <el-button type="danger" text :disabled="busy" @click="revokeApplication(scope.row)">撤销</el-button>
        </template>
      </el-table-column>
    </el-table>
    <p class="gateway-tip">POST /api/wechat/send；请求头 Authorization: Bearer 应用令牌；JSON 请求体如下：</p>
    <pre class="gateway-example">{"text":"应用通知内容"}</pre>
    <el-dialog v-model="tokenDialog" title="请立即保存应用令牌" width="min(560px, 92vw)" @closed="newToken = ''">
      <el-alert title="令牌仅显示这一次。服务端只保存摘要，丢失后请撤销并重新创建。不要发送到聊天中。" type="warning" :closable="false" />
      <el-input v-model="newToken" type="password" show-password readonly class="gateway-space" />
      <template #footer><el-button @click="tokenDialog = false">已保存，关闭</el-button></template>
    </el-dialog>
  </el-card>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

interface WechatAccount {
  id: string
  name: string
  owner: string
  bot_id: string
  connected: boolean
  last_error: string
  is_default: boolean
}

interface GatewayStatus {
  available: boolean
  enabled: boolean
  notifications: boolean
  connected: boolean
  owner: string
  bot_id: string
  login: { status: string; qr_image: string; message: string; account_id?: string }
  last_error: string
  default_account: string
  accounts: WechatAccount[]
  apps: Array<{ id: string; name: string; account_id: string }>
}

const gateway = reactive<GatewayStatus>({
  available: false, enabled: false, notifications: false, connected: false,
  owner: '', bot_id: '', login: { status: 'idle', qr_image: '', message: '' }, last_error: '', apps: [],
  default_account: '', accounts: []
})
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const applicationName = ref('')
const newToken = ref('')
const tokenDialog = ref(false)
const verifyCode = ref('')
const selectedAccount = ref('')
const accountName = ref('')
const currentAccount = computed(() => gateway.accounts.find(account => account.id === selectedAccount.value))
const loginAccountName = computed(() => gateway.accounts.find(account => account.id === gateway.login.account_id)?.name || '无')
const loginPending = computed(() => ['loading', 'wait', 'scaned', 'scaned_but_redirect', 'need_verifycode'].includes(gateway.login.status))
let timer: ReturnType<typeof setTimeout> | undefined
let disposed = false

async function request(path: string, method = 'GET', body?: Record<string, unknown>) {
  const response = await fetch(`/api/wechat${path}`, {
    method, credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
    body: method === 'GET' ? undefined : JSON.stringify(body || {}),
    signal: AbortSignal.timeout(25000)
  })
  if (!response.headers.get('Content-Type')?.includes('application/json')) {
    throw new Error('请重新登录 Web 管理页面')
  }
  const result = await response.json()
  if (!response.ok || !result.success) throw new Error(result.message || '微信请求失败')
  return result
}

async function refresh() {
  const result = await request('/status')
  if (!disposed) {
    Object.assign(gateway, result.data)
    if (!gateway.accounts.some(account => account.id === selectedAccount.value)) selectedAccount.value = gateway.default_account
  }
}

async function perform(operation: () => Promise<void>) {
  busy.value = true
  error.value = ''
  try {
    await operation()
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : '操作失败'
    ElMessage.error(error.value)
  } finally {
    try { await refresh() } catch { error.value = error.value || '无法刷新网关状态' }
    busy.value = false
  }
}

function saveSettings() {
  return perform(async () => {
    await request('/settings', 'POST', { enabled: gateway.enabled, notifications: gateway.notifications })
  })
}

function startLogin() {
  return perform(async () => { await request('/login', 'POST', { account_id: selectedAccount.value }) })
}

function createAccount() {
  return perform(async () => {
    const result = await request('/accounts', 'POST', { name: accountName.value })
    selectedAccount.value = result.data.id
    accountName.value = ''
  })
}

function setDefaultAccount(identifier: string) {
  return perform(async () => { await request(`/accounts/${identifier}/default`, 'POST') })
}

async function removeAccount(account: WechatAccount) {
  try { await ElMessageBox.confirm(`移除“${account.name}”的微信绑定并撤销其所有应用令牌？不会删除共用网盘任务。`, '移除账号') } catch { return }
  return perform(async () => { await request(`/accounts/${account.id}`, 'DELETE') })
}

function submitVerifyCode() {
  return perform(async () => {
    await request('/verify', 'POST', { code: verifyCode.value })
    verifyCode.value = ''
  })
}

function testMessage() {
  return perform(async () => {
    const result = await request('/test', 'POST', { account_id: selectedAccount.value })
    ElMessage.success(result.message)
  })
}

async function logout() {
  try { await ElMessageBox.confirm('清除此账号的本地微信登录凭证？其他微信账号、共用任务和公众号通知不受影响。', '退出微信') } catch { return }
  return perform(async () => { await request('/logout', 'POST', { account_id: selectedAccount.value }) })
}

function createApplication() {
  return perform(async () => {
    const result = await request('/apps', 'POST', { name: applicationName.value, account_id: selectedAccount.value })
    newToken.value = result.data.token
    tokenDialog.value = true
    applicationName.value = ''
  })
}

async function revokeApplication(application: { id: string; name: string }) {
  try { await ElMessageBox.confirm(`撤销“${application.name}”的发送权限？`, '撤销令牌') } catch { return }
  return perform(async () => { await request(`/apps/${application.id}`, 'DELETE') })
}

async function poll() {
  try {
    if (!busy.value && !document.hidden) {
      await refresh()
      error.value = ''
    }
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : '状态读取失败'
  } finally {
    loading.value = false
    if (!disposed) timer = setTimeout(poll, 3000)
  }
}

onMounted(poll)
onUnmounted(() => {
  disposed = true
  if (timer) clearTimeout(timer)
  newToken.value = ''
  verifyCode.value = ''
})
</script>

<style scoped>
.gateway-space { margin-top: 16px; }
.gateway-tip { color: #606266; font-size: 13px; line-height: 1.7; }
.gateway-actions { display: flex; flex-wrap: wrap; gap: 10px; }
.gateway-app-input { max-width: 300px; }
.gateway-id, .gateway-example { overflow-wrap: anywhere; white-space: pre-wrap; }
.gateway-qr { text-align: center; }
</style>
