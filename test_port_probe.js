/*
 * 验证 electron/main.js 里的 probeBackend()：它决定打包版是"用现成的后端"还是"自己启动一个"。
 * 直接从源码里取出真实函数来测，不复制代码，避免测的和跑的不是同一份。
 *
 * 运行：node test_port_probe.js
 */

const fs = require('fs');
const http = require('http');

async function main() {
  const src = fs.readFileSync('electron/main.js', 'utf8');

  // 取出 probeBackend 的源码：从函数开头，到第一行顶格的 '}' 为止
  const start = src.indexOf('function probeBackend()');
  if (start < 0) throw new Error('没能在 main.js 里找到 probeBackend');
  let end = -1;
  let cursor = start;
  while (cursor < src.length) {
    const nl = src.indexOf('\n', cursor);
    if (nl < 0) break;
    if (src.slice(cursor, nl).replace(/\r$/, '') === '}') {
      end = nl + 1;
      break;
    }
    cursor = nl + 1;
  }
  if (end < 0) throw new Error('没能确定 probeBackend 的结尾');
  const fnSource = src.slice(start, end);

  function startFakeServer(handler) {
    return new Promise((resolve) => {
      const server = http.createServer(handler);
      server.listen(0, '127.0.0.1', () => resolve(server));
    });
  }

  function probeFactory(port) {
    const fn = new Function('http', 'HOME', fnSource + '\nreturn probeBackend;');
    return fn(http, `http://127.0.0.1:${port}`);
  }

  function freePort() {
    return new Promise((resolve) => {
      const server = http.createServer();
      server.listen(0, '127.0.0.1', () => {
        const port = server.address().port;
        server.close(() => resolve(port));
      });
    });
  }

  // 1) 对面是自己的后端
  const good = await startFakeServer((req, res) => {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ app: 'pumail', pid: 12345, port: 5211 }));
  });
  const goodPort = good.address().port;
  const gotGood = await probeFactory(goodPort)();
  if (!gotGood || gotGood.app !== 'pumail' || gotGood.pid !== 12345) {
    throw new Error('没认出自己的后端：' + JSON.stringify(gotGood));
  }
  good.close();
  console.log('ok: 能认出自己的后端（拿到 pid）');

  // 2) 对面是别的东西（例如老版本没有 /api/ping，返回鉴权错误）
  const other = await startFakeServer((req, res) => {
    res.writeHead(401, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: '未授权，请从首页打开 PuMail' }));
  });
  const otherPort = other.address().port;
  const gotOther = await probeFactory(otherPort)();
  if (!gotOther || gotOther.other !== true) {
    throw new Error('应该判定为"不是自己的后端"：' + JSON.stringify(gotOther));
  }
  other.close();
  console.log('ok: 对面的东西不是自己后端时，能识别出来');

  // 3) 端口上没人
  const emptyPort = await freePort();
  const gotEmpty = await probeFactory(emptyPort)();
  if (gotEmpty !== null) {
    throw new Error('端口空着时应该返回 null：' + JSON.stringify(gotEmpty));
  }
  console.log('ok: 端口空着时返回 null（会自己启动后端）');

  console.log('ALL OK');
}

main().catch((err) => {
  console.error('测试失败：' + err.message);
  process.exit(1);
});
