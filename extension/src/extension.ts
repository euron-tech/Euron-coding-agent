import * as vscode from 'vscode';
import * as cp from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import WebSocket from 'ws';

const BACKEND_PACKAGE = 'euron-coding-agent';

interface ProviderMeta {
  id: string;
  label: string;
  detail: string;
  needsKey: boolean;
  custom?: boolean;
}

const PROVIDERS: ProviderMeta[] = [
  { id: 'euri', label: 'Euron / Euri', detail: 'api.euron.one', needsKey: true },
  { id: 'openai', label: 'OpenAI', detail: 'GPT models', needsKey: true },
  { id: 'openrouter', label: 'OpenRouter', detail: 'Hundreds of models', needsKey: true },
  { id: 'anthropic', label: 'Anthropic (Claude)', detail: 'Claude models', needsKey: true },
  { id: 'ollama', label: 'Ollama', detail: 'Local, no API key', needsKey: false },
  {
    id: 'custom',
    label: 'Custom (OpenAI-compatible)',
    detail: 'Self-hosted: vLLM, LM Studio, …',
    needsKey: false,
    custom: true
  }
];

const KEY_PROVIDER = 'euronAgent.provider';
const KEY_BASEURL = 'euronAgent.baseUrl';
const KEY_MODEL = 'euronAgent.customModel';
const KEY_AUTOAPPROVE = 'euronAgent.autoApprove';
const KEY_WORKSPACE = 'euronAgent.workspace';
const KEY_PLAN = 'euronAgent.planMode';
const secretKeyFor = (provider: string) => `euronAgent.apiKey:${provider}`;

function providerMeta(id: string): ProviderMeta {
  return PROVIDERS.find((p) => p.id === id) || PROVIDERS[0];
}

// --------------------------------------------------------------------------- //
// Activation
// --------------------------------------------------------------------------- //
export function activate(context: vscode.ExtensionContext) {
  const backend = new BackendManager(context);
  const provider = new ChatViewProvider(context, backend);

  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(ChatViewProvider.viewId, provider, {
      webviewOptions: { retainContextWhenHidden: true }
    }),
    vscode.commands.registerCommand('euronAgent.openChat', () =>
      vscode.commands.executeCommand('workbench.view.extension.euronAgent')
    ),
    vscode.commands.registerCommand('euronAgent.setApiKey', () =>
      configureProvider(context, true)
    ),
    vscode.commands.registerCommand('euronAgent.selectProvider', () =>
      configureProvider(context, false)
    ),
    vscode.commands.registerCommand('euronAgent.restartBackend', async () => {
      backend.stop();
      provider.dropConnection();
      vscode.window.showInformationMessage('Euron Agent backend will restart on next run.');
    }),
    vscode.commands.registerCommand('euronAgent.stop', () => provider.stopTask()),
    vscode.commands.registerCommand('euronAgent.undo', () => provider.undo()),
    vscode.commands.registerCommand('euronAgent.togglePlanMode', async () => {
      const cur = context.globalState.get<boolean>(KEY_PLAN) || false;
      await context.globalState.update(KEY_PLAN, !cur);
      vscode.window.showInformationMessage(
        `Euron Agent plan mode: ${!cur ? 'ON (next task plans first)' : 'off'}`
      );
    }),
    vscode.commands.registerCommand('euronAgent.toggleAutoApprove', async () => {
      const cur = context.globalState.get<boolean>(KEY_AUTOAPPROVE) || false;
      await context.globalState.update(KEY_AUTOAPPROVE, !cur);
      vscode.window.showInformationMessage(`Euron Agent auto-approve: ${!cur ? 'ON' : 'off'}`);
    }),
    vscode.commands.registerCommand('euronAgent.selectWorkspace', async () => {
      const folders = vscode.workspace.workspaceFolders || [];
      if (folders.length < 2) {
        vscode.window.showInformationMessage('Only one workspace folder is open.');
        return;
      }
      const pick = await vscode.window.showQuickPick(
        folders.map((f) => ({ label: f.name, description: f.uri.fsPath })),
        { placeHolder: 'Workspace folder the agent should operate on' }
      );
      if (pick) {
        await context.globalState.update(KEY_WORKSPACE, pick.description);
        vscode.window.showInformationMessage(`Euron Agent workspace: ${pick.label}`);
      }
    }),
    { dispose: () => backend.stop() }
  );
}

export function deactivate() {
  /* backend disposed via subscriptions */
}

// --------------------------------------------------------------------------- //
// Provider / key configuration (stored in SecretStorage + globalState)
// --------------------------------------------------------------------------- //
async function configureProvider(
  context: vscode.ExtensionContext,
  forceKey: boolean
): Promise<string | undefined> {
  const pick = await vscode.window.showQuickPick(
    PROVIDERS.map((p) => ({ label: p.label, description: p.detail, id: p.id })),
    { placeHolder: 'Select the LLM provider for Euron Agent' }
  );
  if (!pick) {
    return undefined;
  }
  const meta = providerMeta(pick.id);
  await context.globalState.update(KEY_PROVIDER, meta.id);

  if (meta.custom) {
    const baseUrl = await vscode.window.showInputBox({
      prompt: 'Base URL of your OpenAI-compatible endpoint',
      value: context.globalState.get<string>(KEY_BASEURL) || 'http://localhost:8001/v1',
      ignoreFocusOut: true
    });
    if (baseUrl) {
      await context.globalState.update(KEY_BASEURL, baseUrl);
    }
    const model = await vscode.window.showInputBox({
      prompt: 'Model id served by that endpoint',
      value: context.globalState.get<string>(KEY_MODEL) || '',
      ignoreFocusOut: true
    });
    if (model) {
      await context.globalState.update(KEY_MODEL, model);
    }
  }

  const existing = await context.secrets.get(secretKeyFor(meta.id));
  if (meta.needsKey || meta.custom || forceKey) {
    const key = await vscode.window.showInputBox({
      prompt: `API key for ${meta.label}` + (meta.needsKey ? '' : ' (optional)'),
      password: true,
      ignoreFocusOut: true,
      placeHolder: existing ? '•••• (leave blank to keep current key)' : ''
    });
    if (key) {
      await context.secrets.store(secretKeyFor(meta.id), key);
    }
  }

  vscode.window.showInformationMessage(`Euron Agent: using ${meta.label}.`);
  return meta.id;
}

async function buildInitPayload(
  context: vscode.ExtensionContext,
  workspacePath: string
): Promise<any | undefined> {
  const providerId = context.globalState.get<string>(KEY_PROVIDER) || 'euri';
  const meta = providerMeta(providerId);
  const apiKey = await context.secrets.get(secretKeyFor(providerId));

  if (meta.needsKey && !apiKey) {
    const choice = await vscode.window.showWarningMessage(
      `No API key set for ${meta.label}.`,
      'Set API Key'
    );
    if (choice) {
      await configureProvider(context, true);
    }
    return undefined;
  }

  const cfg = vscode.workspace.getConfiguration('euronAgent');
  const settingModel = cfg.get<string>('model');
  const customModel = context.globalState.get<string>(KEY_MODEL);
  return {
    type: 'init',
    workspace_path: workspacePath,
    provider: providerId,
    api_key: apiKey || undefined,
    base_url: meta.custom ? context.globalState.get<string>(KEY_BASEURL) : undefined,
    model: settingModel || (meta.custom ? customModel : undefined) || undefined,
    auto_approve: context.globalState.get<boolean>(KEY_AUTOAPPROVE) || false,
    persist: cfg.get<boolean>('persistHistory') ?? true,
    plan_mode: context.globalState.get<boolean>(KEY_PLAN) || false
  };
}

// --------------------------------------------------------------------------- //
// Backend lifecycle: detect Python, provision a private venv, install + serve
// --------------------------------------------------------------------------- //
function runProcess(
  command: string,
  args: string[],
  options: cp.SpawnOptions = {}
): Promise<{ code: number; stdout: string; stderr: string }> {
  return new Promise((resolve) => {
    let stdout = '';
    let stderr = '';
    const child = cp.spawn(command, args, options);
    child.stdout?.on('data', (d) => (stdout += d.toString()));
    child.stderr?.on('data', (d) => (stderr += d.toString()));
    child.on('error', () => resolve({ code: -1, stdout, stderr }));
    child.on('close', (code) => resolve({ code: code ?? -1, stdout, stderr }));
  });
}

class BackendManager {
  private process?: cp.ChildProcess;
  private wsUrl?: string;
  private startPromise?: Promise<string | undefined>;
  private _token?: string;

  constructor(private readonly context: vscode.ExtensionContext) {}

  get token(): string | undefined {
    const override = vscode.workspace.getConfiguration('euronAgent').get<string>('token');
    return override || this._token;
  }

  private get storageDir(): string {
    const dir = this.context.globalStorageUri.fsPath;
    fs.mkdirSync(dir, { recursive: true });
    return dir;
  }

  private get venvPython(): string {
    const venv = path.join(this.storageDir, 'backend-venv');
    return process.platform === 'win32'
      ? path.join(venv, 'Scripts', 'python.exe')
      : path.join(venv, 'bin', 'python');
  }

  stop() {
    if (this.process) {
      try {
        this.process.kill();
      } catch {
        /* ignore */
      }
      this.process = undefined;
    }
    this.wsUrl = undefined;
    this._token = undefined;
    this.startPromise = undefined;
  }

  /** Returns a WebSocket URL to a ready backend, starting one if needed. */
  async getWsUrl(): Promise<string | undefined> {
    const override = vscode.workspace.getConfiguration('euronAgent').get<string>('serverUrl');
    if (override) {
      return override; // developer-managed backend
    }
    if (this.wsUrl && this.process && !this.process.killed) {
      return this.wsUrl;
    }
    if (!this.startPromise) {
      this.startPromise = this.startManaged().finally(() => (this.startPromise = undefined));
    }
    return this.startPromise;
  }

  private async detectPython(): Promise<{ cmd: string; pre: string[] } | undefined> {
    const configured = vscode.workspace.getConfiguration('euronAgent').get<string>('pythonPath');
    const candidates: { cmd: string; pre: string[] }[] = [];
    if (configured) {
      candidates.push({ cmd: configured, pre: [] });
    }
    candidates.push({ cmd: 'python3', pre: [] }, { cmd: 'python', pre: [] }, { cmd: 'py', pre: ['-3'] });

    for (const c of candidates) {
      const r = await runProcess(c.cmd, [
        ...c.pre,
        '-c',
        'import sys;print(sys.version_info[0]*100+sys.version_info[1])'
      ]);
      const v = parseInt(r.stdout.trim(), 10);
      if (r.code === 0 && v >= 309) {
        return c;
      }
    }
    return undefined;
  }

  private async provision(): Promise<string | undefined> {
    const py = await this.detectPython();
    if (!py) {
      const choice = await vscode.window.showErrorMessage(
        'Euron Agent needs Python 3.9+ to run its backend, but none was found.',
        'Install Python',
        'Set Python Path'
      );
      if (choice === 'Install Python') {
        vscode.env.openExternal(vscode.Uri.parse('https://www.python.org/downloads/'));
      } else if (choice === 'Set Python Path') {
        vscode.commands.executeCommand('workbench.action.openSettings', 'euronAgent.pythonPath');
      }
      return undefined;
    }

    const venvDir = path.join(this.storageDir, 'backend-venv');
    const freshVenv = !fs.existsSync(this.venvPython);
    const pinned = vscode.workspace.getConfiguration('euronAgent').get<string>('backendVersion') || '';
    const extVersion = this.context.extension.packageJSON.version as string;
    const markerPath = path.join(this.storageDir, 'backend.json');

    let needInstall = freshVenv;
    if (!freshVenv) {
      try {
        const marker = JSON.parse(fs.readFileSync(markerPath, 'utf-8'));
        needInstall = marker.extVersion !== extVersion || marker.pinned !== pinned;
      } catch {
        needInstall = true;
      }
    }

    const spec = pinned ? `${BACKEND_PACKAGE}==${pinned}` : BACKEND_PACKAGE;
    const ok = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: 'Euron Agent', cancellable: false },
      async (progress) => {
        if (freshVenv) {
          progress.report({ message: 'creating Python environment…' });
          const r = await runProcess(py.cmd, [...py.pre, '-m', 'venv', venvDir]);
          if (r.code !== 0) {
            vscode.window.showErrorMessage('Euron Agent: failed to create venv. ' + r.stderr.slice(0, 300));
            return false;
          }
        }
        if (needInstall) {
          progress.report({ message: `installing ${spec} from PyPI…` });
          const r = await runProcess(this.venvPython, [
            '-m',
            'pip',
            'install',
            '--upgrade',
            '--disable-pip-version-check',
            spec
          ]);
          if (r.code !== 0) {
            vscode.window.showErrorMessage('Euron Agent: backend install failed. ' + r.stderr.slice(-400));
            return false;
          }
          fs.writeFileSync(markerPath, JSON.stringify({ extVersion, pinned }));
        }
        return true;
      }
    );
    return ok ? this.venvPython : undefined;
  }

  private async startManaged(): Promise<string | undefined> {
    const python = await this.provision();
    if (!python) {
      return undefined;
    }
    return new Promise<string | undefined>((resolve) => {
      const child = cp.spawn(python, ['-m', 'euron_agent', 'serve', '--port', '0'], {
        cwd: this.storageDir,
        env: process.env
      });
      this.process = child;
      let buffer = '';
      let settled = false;

      const finish = (url?: string) => {
        if (!settled) {
          settled = true;
          resolve(url);
        }
      };

      child.stdout?.on('data', (d) => {
        buffer += d.toString();
        const tok = buffer.match(/EURON_AGENT_TOKEN (\S+)/);
        if (tok) {
          this._token = tok[1];
        }
        const m = buffer.match(/EURON_AGENT_LISTENING (http:\/\/\S+)/);
        if (m) {
          this.wsUrl = m[1].replace(/^http/, 'ws') + '/ws';
          finish(this.wsUrl);
        }
      });
      child.stderr?.on('data', (d) => console.log('[euron-agent backend]', d.toString()));
      child.on('exit', (code) => {
        console.log('[euron-agent backend] exited', code);
        if (this.process === child) {
          this.process = undefined;
          this.wsUrl = undefined;
        }
        finish(undefined);
      });
      setTimeout(() => finish(this.wsUrl), 20000);
    });
  }
}

// --------------------------------------------------------------------------- //
// Webview + WebSocket bridge
// --------------------------------------------------------------------------- //
class ChatViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewId = 'euronAgent.chat';
  private view?: vscode.WebviewView;
  private ws?: WebSocket;
  private connecting = false;

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly backend: BackendManager
  ) {}

  resolveWebviewView(view: vscode.WebviewView) {
    this.view = view;
    view.webview.options = { enableScripts: true };
    view.webview.html = this.getHtml(view.webview);
    view.webview.onDidReceiveMessage(async (msg) => {
      switch (msg.command) {
        case 'run':
          await this.runTask(msg.text);
          break;
        case 'approval':
          this.send({ type: 'approval', id: msg.id, approved: msg.approved, feedback: msg.feedback });
          break;
        case 'cancel':
          this.stopTask();
          break;
        case 'undo':
          this.undo();
          break;
      }
    });
  }

  stopTask() {
    this.send({ type: 'cancel' });
  }

  undo() {
    this.send({ type: 'undo' });
  }

  private getWorkspace(): string | undefined {
    const folders = vscode.workspace.workspaceFolders || [];
    if (folders.length === 0) {
      return undefined;
    }
    const chosen = this.context.globalState.get<string>(KEY_WORKSPACE);
    if (chosen && folders.some((f) => f.uri.fsPath === chosen)) {
      return chosen;
    }
    return folders[0].uri.fsPath;
  }

  dropConnection() {
    if (this.ws) {
      try {
        this.ws.close();
      } catch {
        /* ignore */
      }
      this.ws = undefined;
    }
  }

  private post(event: any) {
    this.view?.webview.postMessage(event);
  }

  private send(obj: any) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj));
    }
  }

  private async runTask(text: string) {
    const workspace = this.getWorkspace();
    if (!workspace) {
      this.post({ type: 'error', message: 'Open a folder/workspace first.' });
      this.post({ type: 'done' });
      return;
    }
    const init = await buildInitPayload(this.context, workspace);
    if (!init) {
      this.post({ type: 'error', message: 'Provider not configured.' });
      this.post({ type: 'done' });
      return;
    }
    if (!(await this.ensureConnected())) {
      this.post({ type: 'done' });
      return;
    }
    init.token = this.backend.token;
    this.send(init);
    this.send({ type: 'run', task: text });
  }

  private async ensureConnected(): Promise<boolean> {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      return true;
    }
    if (this.connecting) {
      return false;
    }
    this.connecting = true;
    try {
      this.post({ type: 'status', message: 'starting backend…' });
      const url = await this.backend.getWsUrl();
      if (!url) {
        this.post({ type: 'error', message: 'Backend unavailable. See notifications.' });
        return false;
      }
      for (let i = 0; i < 30; i++) {
        if (await this.tryConnect(url)) {
          return true;
        }
        await new Promise((r) => setTimeout(r, 400));
      }
      this.post({ type: 'error', message: `Could not connect to ${url}.` });
      return false;
    } finally {
      this.connecting = false;
    }
  }

  private tryConnect(url: string): Promise<boolean> {
    return new Promise((resolve) => {
      let settled = false;
      const ws = new WebSocket(url);
      const done = (ok: boolean) => {
        if (!settled) {
          settled = true;
          resolve(ok);
        }
      };
      ws.on('open', () => {
        this.ws = ws;
        ws.on('message', (data) => {
          try {
            this.post(JSON.parse(data.toString()));
          } catch {
            /* ignore */
          }
        });
        ws.on('close', () => {
          if (this.ws === ws) {
            this.ws = undefined;
          }
        });
        this.post({ type: 'status', message: 'connected' });
        done(true);
      });
      ws.on('error', () => done(false));
      setTimeout(() => done(false), 1500);
    });
  }

  private getHtml(webview: vscode.Webview): string {
    const nonce = String(Math.random()).slice(2) + String(Date.now());
    const scriptUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this.context.extensionUri, 'media', 'main.js')
    );
    const styleUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this.context.extensionUri, 'media', 'style.css')
    );
    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="Content-Security-Policy"
    content="default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}';" />
  <link href="${styleUri}" rel="stylesheet" />
  <title>Euron Agent</title>
</head>
<body>
  <div id="topbar">
    <span id="tokens"></span>
    <button id="undo" title="Revert the last task's file changes">Undo</button>
  </div>
  <div id="log"></div>
  <div id="composer">
    <textarea id="prompt" rows="3" placeholder="Ask Euron Agent to change your code… (Ctrl/Cmd+Enter)"></textarea>
    <button id="send">Run</button>
    <button id="stop" style="display:none;">Stop</button>
  </div>
  <script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
  }
}
