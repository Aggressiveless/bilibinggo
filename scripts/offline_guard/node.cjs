if (process.env.BINGGO_OFFLINE === '1') {
  const net = require('node:net');
  const original = net.Socket.prototype.connect;
  net.Socket.prototype.connect = function (...args) {
    const normalized = Array.isArray(args[0]) ? args[0] : args;
    const option = normalized[0];
    const host = typeof option === 'object' ? option.host : typeof normalized[1] === 'string' ? normalized[1] : 'localhost';
    if (host && !['localhost', '127.0.0.1', '::1'].includes(host)) {
      throw new Error('Offline worker blocked an external socket');
    }
    return original.apply(this, args);
  };
}
