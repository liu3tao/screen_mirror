// 窗景找房：单页 UI 状态与接口调用（Alpine.js）

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body instanceof FormData) {
    opts.body = body;
  } else if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || data.detail || `HTTP ${res.status}`);
    err.hint = data.hint || '';
    err.runId = data.run_id;
    throw err;
  }
  return data;
}

const ACTIVE = ['searching', 'downloading', 'scoring'];

function dreamview() {
  return {
    config: { scope_tags: [], image_search: 'ddgs' },
    runs: [],
    runId: '',
    state: null,
    scene: null,
    regions: null,
    scopes: ['全球'],
    scopeFree: '',
    manual: { city: '', district: '', keywords: '', search_region: 'wt-wt' },
    filter: { min: 0, regions: [], sources: [] },
    busy: { upload: false, scene: false, regions: false, stop: false },
    dragOver: false,
    error: '',
    hint: '',
    timer: null,
    running: false,

    async init() {
      this.config = await api('GET', '/api/config');
      await this.refreshRuns();
    },

    fail(e) {
      this.error = e.message;
      this.hint = e.hint || '';
    },

    async refreshRuns() {
      this.runs = await api('GET', '/api/runs');
    },

    newRun() {
      this.stopPolling();
      this.runId = '';
      this.state = this.scene = this.regions = null;
      this.filter = { min: 0, regions: [], sources: [] };
    },

    async openRun(id) {
      if (!id) return this.newRun();
      try {
        const d = await api('GET', `/api/runs/${id}`);
        this.runId = id;
        this.apply(d, true);
        if (this.running || ACTIVE.includes(this.state.status)) this.startPolling();
      } catch (e) { this.fail(e); }
    },

    apply(d, full) {
      this.state = d.state;
      this.running = d.running;
      if (full) {
        this.scene = d.scene.elements.length ? d.scene : null;
        this.regions = d.regions.regions.length || d.regions.raw_text ? d.regions : null;
        if (d.regions.scopes.length) {
          this.scopes = d.regions.scopes.filter((s) => this.config.scope_tags.includes(s));
          this.scopeFree = d.regions.scopes.filter((s) => !this.config.scope_tags.includes(s)).join('，');
        }
      }
    },

    async upload(file) {
      if (!file) return;
      this.busy.upload = true;
      this.error = '';
      const fd = new FormData();
      fd.append('file', file);
      try {
        const d = await api('POST', '/api/runs', fd);
        this.runId = d.state.id;
        this.apply(d, true);
      } catch (e) {
        this.fail(e);
      } finally {
        this.busy.upload = false;
        await this.refreshRuns();
      }
    },

    splitList(text) { return text.split(/[,，、;；\n]/).map((s) => s.trim()).filter(Boolean); },

    async saveScene() {
      this.busy.scene = true;
      try {
        this.scene = await api('PUT', `/api/runs/${this.runId}/scene`, this.scene);
      } catch (e) { this.fail(e); throw e; } finally { this.busy.scene = false; }
    },

    allScopes() {
      const all = [...this.scopes, ...this.splitList(this.scopeFree)];
      // 选了具体范围时「全球」无意义
      return all.length > 1 ? all.filter((s) => s !== '全球') : all;
    },

    async findRegions() {
      this.busy.regions = true;
      this.error = '';
      try {
        await this.saveScene();
        if (this.regions) await this.saveRegions();
        this.regions = await api('POST', `/api/runs/${this.runId}/regions`, { scopes: this.allScopes() });
        await this.reloadState();
      } catch (e) { this.fail(e); } finally { this.busy.regions = false; }
    },

    async saveRegions() {
      try {
        const d = await api('PUT', `/api/runs/${this.runId}/regions`, { regions: this.regions.regions });
        this.regions.regions = d.regions;
      } catch (e) { this.fail(e); throw e; }
    },

    setAll(on) { this.regions.regions.forEach((r) => { r.selected = on; }); },

    addManual() {
      if (!this.manual.city.trim()) return;
      if (!this.regions) {
        this.regions = { scopes: [], grounded: true, regions: [], raw_text: '', search_suggestions_html: '', citations: [], web_search_queries: [], parse_error: '' };
      }
      this.regions.regions.push({
        id: 'm' + Date.now().toString(36),
        city: this.manual.city.trim(),
        district: this.manual.district.trim(),
        reason: '', months: '',
        keywords: this.splitList(this.manual.keywords),
        search_region: this.manual.search_region.trim() || 'wt-wt',
        selected: true, manual: true,
      });
      this.manual = { city: '', district: '', keywords: '', search_region: 'wt-wt' };
    },

    selectedCount() {
      return this.regions ? this.regions.regions.filter((r) => r.selected).length : 0;
    },

    estimatedSearches() {
      let n = 0;
      if (this.regions) {
        for (const r of this.regions.regions) if (r.selected) n += Math.min(3, r.keywords.length);
      }
      return n;
    },

    // ② 完成（勾了地区）或已有结果时才显示 ③
    showWall() {
      return this.selectedCount() > 0 || this.isRunning() || (this.state?.items || []).length > 0;
    },

    // 搜图按钮不可用时，告诉用户下一步做什么
    searchHint() {
      if (this.estimatedSearches() > 0) return '';
      if (!this.selectedCount()) return '请在上方地区卡片中勾选至少一个地区。';
      return '已勾选的地区没有关键词：请在地区卡片里填写关键词。';
    },

    isRunning() {
      return this.running || (this.state && ACTIVE.includes(this.state.status));
    },

    async startSearch() {
      this.error = '';
      try {
        await this.saveScene();
        if (this.regions) await this.saveRegions();
        await api('POST', `/api/runs/${this.runId}/search`);
        this.running = true;
        this.startPolling();
      } catch (e) { this.fail(e); }
    },

    async stopSearch() {
      this.busy.stop = true;
      try {
        await api('POST', `/api/runs/${this.runId}/stop`);
      } catch (e) { this.fail(e); this.busy.stop = false; }
    },

    async reloadState() {
      const d = await api('GET', `/api/runs/${this.runId}`);
      this.apply(d, false);
      return d;
    },

    startPolling() {
      this.stopPolling();
      this.timer = setInterval(async () => {
        try {
          const d = await this.reloadState();
          if (!d.running && !ACTIVE.includes(d.state.status)) {
            this.stopPolling();
            this.busy.stop = false;
            if (d.state.status === 'error') { this.error = d.state.error; this.hint = d.state.error_hint; }
            this.refreshRuns();
          }
        } catch (e) { this.stopPolling(); this.fail(e); }
      }, 1500);
    },

    stopPolling() {
      if (this.timer) clearInterval(this.timer);
      this.timer = null;
    },

    backendLabel() {
      return { gemini: 'Gemini API', vertex: 'Vertex AI', ollama: 'Ollama（本地）' }[this.config.backend] || this.config.backend || '';
    },

    usageText() {
      const u = this.state.usage;
      const cost = this.config.backend === 'ollama' ? '本地免费' : `约 $${u.usd.toFixed(3)}`;
      return `模型调用 ${u.calls} 次 · 输入 ${u.input_tokens} / 输出 ${u.output_tokens + u.thought_tokens} token · ${cost}`;
    },

    wallRegions() {
      return [...new Set((this.state?.items || []).map((it) => it.region_label))];
    },

    wall() {
      const items = (this.state?.items || []).filter((it) => {
        if (this.filter.min > 0 && (it.score === null || it.score < this.filter.min)) return false;
        if (this.filter.regions.length && !this.filter.regions.includes(it.region_label)) return false;
        if (this.filter.sources.length && !this.filter.sources.includes(it.source_type)) return false;
        return true;
      });
      return items.sort((a, b) => (a.score === null) - (b.score === null) || (b.score || 0) - (a.score || 0));
    },
  };
}
