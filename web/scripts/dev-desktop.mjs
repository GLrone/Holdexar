import { createLogger, createServer } from 'vite'

const server = await createServer({
  customLogger: createLogger('info', { console: { ...console, log: console.error } }),
  server: { host: '127.0.0.1', port: 0 },
})
process.stdin.once('end', async () => {
  await server.close()
  process.exit(0)
})
process.stdin.resume()
await server.listen()
process.stdout.write(`${server.httpServer.address().port}\n`)
