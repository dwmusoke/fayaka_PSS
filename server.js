// Compatibility shim: the ERP server is implemented in Python (server.py).
// Run: npm run dev  OR  python server.py
const { spawn } = require('child_process');
const py = process.platform === 'win32' ? 'python' : 'python3';
const child = spawn(py, ['server.py'], { stdio: 'inherit', cwd: __dirname, env: process.env });
child.on('exit', (code) => process.exit(code == null ? 1 : code));
child.on('error', (err) => {
  console.error('Failed to start Python server:', err.message);
  console.error('Run directly with: python server.py');
  process.exit(1);
});
