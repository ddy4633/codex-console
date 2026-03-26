/**
 * Grok 页面脚本
 */

let currentTaskUuid = null;
let currentBatchId = null;
let taskPoller = null;
let batchPoller = null;
let lastRenderedLogs = [];

const elements = {
    form: document.getElementById('grok-form'),
    emailService: document.getElementById('email-service'),
    vibemailJwt: document.getElementById('vibemail-jwt'),
    vibemailApi: document.getElementById('vibemail-api'),
    regMode: document.getElementById('reg-mode'),
    batchCountGroup: document.getElementById('batch-count-group'),
    batchOptions: document.getElementById('batch-options'),
    batchCount: document.getElementById('batch-count'),
    intervalMin: document.getElementById('interval-min'),
    intervalMax: document.getElementById('interval-max'),
    password: document.getElementById('password'),
    proxy: document.getElementById('proxy'),
    userDataDir: document.getElementById('user-data-dir'),
    userAgent: document.getElementById('user-agent'),
    cfClearance: document.getElementById('cf-clearance'),
    cfBm: document.getElementById('cf-bm'),
    cfCookie: document.getElementById('cf-cookie'),
    startBtn: document.getElementById('start-btn'),
    cancelBtn: document.getElementById('cancel-btn'),
    taskId: document.getElementById('task-id'),
    taskStatus: document.getElementById('task-status'),
    taskEmail: document.getElementById('task-email'),
    taskMode: document.getElementById('task-mode'),
    batchProgress: document.getElementById('batch-progress'),
    batchTotal: document.getElementById('batch-total'),
    batchCompleted: document.getElementById('batch-completed'),
    batchSuccess: document.getElementById('batch-success'),
    batchFailed: document.getElementById('batch-failed'),
    consoleLog: document.getElementById('console-log'),
    clearLogBtn: document.getElementById('clear-log-btn'),
    recentAccountsTable: document.getElementById('recent-accounts-table'),
    refreshAccountsBtn: document.getElementById('refresh-accounts-btn'),
};

document.addEventListener('DOMContentLoaded', () => {
    initEventListeners();
    loadAvailableServices();
    loadRecentAccounts();
});

function initEventListeners() {
    elements.form.addEventListener('submit', handleSubmit);
    elements.regMode.addEventListener('change', handleModeChange);
    elements.cancelBtn.addEventListener('click', handleCancel);
    elements.clearLogBtn.addEventListener('click', () => {
        lastRenderedLogs = [];
        elements.consoleLog.innerHTML = '<div class="log-line info">[系统] 日志已清空</div>';
    });
    elements.refreshAccountsBtn.addEventListener('click', loadRecentAccounts);
}

async function loadAvailableServices() {
    try {
        const data = await api.get('/registration/available-services');
        renderServiceOptions(data);
    } catch (error) {
        toast.error('加载邮箱服务失败: ' + error.message);
    }
}

function renderServiceOptions(data) {
    elements.emailService.innerHTML = '';
    const serviceOrder = ['tempmail', 'imap_mail', 'temp_mail', 'duck_mail', 'freemail', 'moe_mail', 'outlook'];

    const vibemailGroup = document.createElement('optgroup');
    vibemailGroup.label = '本地 Grok (Vibemail)';
    const vibemailOption = document.createElement('option');
    vibemailOption.value = 'vibemail:default';
    vibemailOption.textContent = 'Vibemail JWT / 系统设置';
    vibemailGroup.appendChild(vibemailOption);
    elements.emailService.appendChild(vibemailGroup);

    serviceOrder.forEach((serviceType) => {
        const item = data[serviceType];
        if (!item || !item.available || !Array.isArray(item.services) || item.services.length === 0) {
            return;
        }

        const group = document.createElement('optgroup');
        group.label = `${getServiceTypeText(serviceType)} (${item.count})`;

        item.services.forEach((service) => {
            const option = document.createElement('option');
            option.value = `${serviceType}:${service.id || 'default'}`;
            const extras = [];
            if (service.email) extras.push(service.email);
            if (service.host) extras.push(service.host);
            if (service.domain) extras.push(`@${service.domain}`);
            if (service.default_domain) extras.push(`@${service.default_domain}`);
            option.textContent = extras.length > 0 ? `${service.name} (${extras.join(' | ')})` : service.name;
            group.appendChild(option);
        });

        elements.emailService.appendChild(group);
    });

    if (!elements.emailService.value) {
        toast.warning('当前没有可用于 Grok 的邮箱服务，请先在邮箱服务页面配置');
    }
}

function handleModeChange() {
    const isBatch = elements.regMode.value === 'batch';
    elements.batchCountGroup.style.display = isBatch ? 'block' : 'none';
    elements.batchOptions.style.display = isBatch ? 'block' : 'none';
    elements.taskMode.textContent = isBatch ? '批量' : '单次';
    elements.batchProgress.style.display = isBatch ? 'block' : 'none';
}

function buildPayload() {
    const selected = elements.emailService.value;
    if (!selected) {
        throw new Error('请先选择邮箱服务');
    }
    const [email_service_type, rawServiceId] = selected.split(':');

    const payload = {
        email_service_type,
        proxy: elements.proxy.value.trim() || null,
        password: elements.password.value.trim() || null,
        vibemail_user_jwt: elements.vibemailJwt.value.trim() || null,
        vibemail_api: elements.vibemailApi.value.trim() || null,
        user_data_dir: elements.userDataDir.value.trim(),
        user_agent: elements.userAgent.value.trim(),
        cf_clearance: elements.cfClearance.value.trim(),
        cf_bm: elements.cfBm.value.trim(),
        cf_cookie_header: elements.cfCookie.value.trim(),
    };

    if (rawServiceId && rawServiceId !== 'default') {
        payload.email_service_id = parseInt(rawServiceId, 10);
    }

    return payload;
}

async function handleSubmit(event) {
    event.preventDefault();

    let payload;
    try {
        payload = buildPayload();
    } catch (error) {
        toast.error(error.message);
        return;
    }

    resetPolling();
    lastRenderedLogs = [];
    elements.consoleLog.innerHTML = '<div class="log-line info">[系统] Grok 任务已创建，等待执行...</div>';
    elements.startBtn.disabled = true;
    elements.cancelBtn.disabled = false;
    elements.taskEmail.textContent = '-';
    elements.taskStatus.textContent = '等待中';

    try {
        if (elements.regMode.value === 'batch') {
            payload.count = parseInt(elements.batchCount.value, 10) || 1;
            payload.interval_min = parseInt(elements.intervalMin.value, 10) || 0;
            payload.interval_max = parseInt(elements.intervalMax.value, 10) || payload.interval_min;
            const data = await api.post('/grok/batch-start', payload);
            currentBatchId = data.batch_id;
            elements.taskId.textContent = currentBatchId.slice(0, 8);
            elements.taskMode.textContent = '批量';
            elements.batchProgress.style.display = 'block';
            startBatchPolling();
            toast.success('Grok 批量任务已启动');
        } else {
            const data = await api.post('/grok/start', payload);
            currentTaskUuid = data.task_uuid;
            elements.taskId.textContent = currentTaskUuid.slice(0, 8);
            elements.taskMode.textContent = '单次';
            elements.batchProgress.style.display = 'none';
            startTaskPolling();
            toast.success('Grok 任务已启动');
        }
    } catch (error) {
        elements.startBtn.disabled = false;
        elements.cancelBtn.disabled = true;
        toast.error('启动 Grok 任务失败: ' + error.message);
    }
}

function startTaskPolling() {
    if (!currentTaskUuid) return;
    pollTask();
    taskPoller = setInterval(pollTask, 2000);
}

async function pollTask() {
    if (!currentTaskUuid) return;
    try {
        const data = await api.get(`/grok/tasks/${currentTaskUuid}`);
        renderTask(data);
        if (['completed', 'failed', 'cancelled'].includes(data.status)) {
            stopTaskPolling();
            elements.startBtn.disabled = false;
            elements.cancelBtn.disabled = true;
            loadRecentAccounts();
        }
    } catch (error) {
        stopTaskPolling();
        elements.startBtn.disabled = false;
        elements.cancelBtn.disabled = true;
        toast.error('获取 Grok 任务状态失败: ' + error.message);
    }
}

function renderTask(data) {
    elements.taskStatus.textContent = getStatusText('task', data.status);
    if (data.result?.email) {
        elements.taskEmail.textContent = data.result.email;
    }
    renderLogs(data.logs || []);
}

function startBatchPolling() {
    if (!currentBatchId) return;
    pollBatch();
    batchPoller = setInterval(pollBatch, 2500);
}

async function pollBatch() {
    if (!currentBatchId) return;
    try {
        const data = await api.get(`/grok/batches/${currentBatchId}`);
        elements.taskStatus.textContent = getStatusText('task', data.status || 'running');
        elements.batchTotal.textContent = data.total || data.tasks.length || 0;
        elements.batchCompleted.textContent = data.completed || 0;
        elements.batchSuccess.textContent = data.success || 0;
        elements.batchFailed.textContent = data.failed || 0;
        renderLogs(data.logs || []);

        const lastCompletedTask = (data.tasks || []).findLast?.((task) => task.result?.email) || null;
        if (lastCompletedTask?.result?.email) {
            elements.taskEmail.textContent = lastCompletedTask.result.email;
        }

        if (data.finished || ['completed', 'cancelled'].includes(data.status)) {
            stopBatchPolling();
            elements.startBtn.disabled = false;
            elements.cancelBtn.disabled = true;
            loadRecentAccounts();
        }
    } catch (error) {
        stopBatchPolling();
        elements.startBtn.disabled = false;
        elements.cancelBtn.disabled = true;
        toast.error('获取 Grok 批量状态失败: ' + error.message);
    }
}

function renderLogs(logs) {
    if (JSON.stringify(logs) === JSON.stringify(lastRenderedLogs)) {
        return;
    }
    lastRenderedLogs = logs.slice();
    if (logs.length === 0) {
        elements.consoleLog.innerHTML = '<div class="log-line info">[系统] 暂无日志</div>';
        return;
    }
    elements.consoleLog.innerHTML = logs.map((log) => `<div class="log-line info">${escapeHtml(log)}</div>`).join('');
    elements.consoleLog.scrollTop = elements.consoleLog.scrollHeight;
}

async function handleCancel() {
    try {
        if (currentTaskUuid) {
            await api.post(`/grok/tasks/${currentTaskUuid}/cancel`, {});
            toast.info('取消请求已发送');
        } else if (currentBatchId) {
            await api.post(`/grok/batches/${currentBatchId}/cancel`, {});
            toast.info('批量取消请求已发送');
        }
    } catch (error) {
        toast.error('取消 Grok 任务失败: ' + error.message);
    }
}

function resetPolling() {
    stopTaskPolling();
    stopBatchPolling();
    currentTaskUuid = null;
    currentBatchId = null;
}

function stopTaskPolling() {
    if (taskPoller) {
        clearInterval(taskPoller);
        taskPoller = null;
    }
}

function stopBatchPolling() {
    if (batchPoller) {
        clearInterval(batchPoller);
        batchPoller = null;
    }
}

async function loadRecentAccounts() {
    try {
        const data = await api.get('/grok/recent-accounts?limit=20');
        const accounts = data.accounts || [];
        if (accounts.length === 0) {
            elements.recentAccountsTable.innerHTML = '<tr><td colspan="4">暂无 Grok 账号</td></tr>';
            return;
        }

        elements.recentAccountsTable.innerHTML = accounts.map((account) => `
            <tr>
                <td>${escapeHtml(account.email)}</td>
                <td>${getStatusText('account', account.status)}</td>
                <td>${escapeHtml(account.source || '-')}</td>
                <td>${format.date(account.created_at)}</td>
            </tr>
        `).join('');
    } catch (error) {
        elements.recentAccountsTable.innerHTML = `<tr><td colspan="4">加载失败: ${escapeHtml(error.message)}</td></tr>`;
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text || '';
    return div.innerHTML;
}
