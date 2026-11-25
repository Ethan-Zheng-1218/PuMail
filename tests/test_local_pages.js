/* Verify desktop help/guide resources in development and packaged layouts. */
const assert = require('assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const vm = require('vm');
const { fileURLToPath } = require('url');

const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'electron', 'main.js'), 'utf8');
const end = source.indexOf('// ---------- 任务栏闪烁通知 ----------');
assert(end > 0, 'Cannot locate the local-page handler');

function renderGuide(html, baseURI, markdown) {
  const target = { innerHTML: '' };
  const context = vm.createContext({
    URL,
    window: { __GUIDE_MD__: markdown },
    localStorage: { getItem: () => null },
    document: {
      baseURI,
      documentElement: { classList: { add() {} } },
      getElementById: () => target,
    },
  });
  for (const [, script] of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) {
    vm.runInContext(script, context);
  }
  const images = [...target.innerHTML.matchAll(/<img src="([^"]+)"/g)].map(m => m[1]);
  assert.equal(images.length, 26, 'Every guide illustration should render');
  return images;
}

async function main() {
  // Include spaces and Chinese characters to exercise real Windows install paths.
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'PuMail 说明 '));
  try {
    const project = path.join(temporary, 'project');
    const resources = path.join(temporary, 'resources');
    const pageSource = path.join(root, 'frontend');
    const devBase = path.join(project, 'dist', 'PuMail', '_internal', 'frontend');
    const packedBase = path.join(resources, 'pumail-server', '_internal', 'frontend');
    fs.cpSync(pageSource, devBase, { recursive: true });
    fs.cpSync(pageSource, packedBase, { recursive: true });

    for (const isPackaged of [false, true]) {
      const handlers = new Map();
      const opened = [];
      const electron = {
        app: { isPackaged },
        ipcMain: { handle: (name, fn) => handlers.set(name, fn) },
        shell: { openExternal: async url => opened.push(url) },
      };
      vm.runInNewContext(source.slice(0, end), {
        require: name => name === 'electron' ? electron
          : name === 'os' ? { tmpdir: () => temporary } : require(name),
        __dirname: path.join(project, 'electron'),
        process: { resourcesPath: resources, env: {}, platform: process.platform },
      });
      const open = handlers.get('open-local-page');
      for (const page of ['help.html', 'privacy.html', 'guide.html']) {
        const result = await open(null, page);
        assert.equal(result.ok, true, result.error);
        const url = opened.at(-1);
        const file = fileURLToPath(url);
        assert(fs.existsSync(file), page);
        if (page === 'guide.html') {
          const html = fs.readFileSync(file, 'utf8');
          for (const [, target] of html.matchAll(/href="(file:[^"]+)"/g)) {
            assert(fs.existsSync(fileURLToPath(target)), target);
          }
          for (const image of renderGuide(html, url)) {
            assert(fs.existsSync(fileURLToPath(image)), image);
          }
        }
      }
      assert.equal((await open(null, 'missing.html')).ok, false);
      console.log('ok: local help, privacy and all guide images (' +
        (isPackaged ? 'packaged' : 'development') + ')');
    }

    const html = fs.readFileSync(path.join(pageSource, 'guide.html'), 'utf8');
    const markdown = fs.readFileSync(path.join(pageSource, 'guides', '邮箱绑定指南.md'), 'utf8');
    for (const image of renderGuide(html, 'http://127.0.0.1:5000/guide.html', markdown)) {
      assert(image.startsWith('http://127.0.0.1:5000/guides/guide-images/'), image);
    }
    console.log('ok: web guide image URLs');
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
