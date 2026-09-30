import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';

export const BRIEF = {
  business_name: 'Review Fixture Plumbing', description: 'Residential fixture plumbing service.',
  contact_email: 'fixture@example.com', location: 'Portland, Oregon',
  services: [{ name: 'Repairs', description: 'Repair household plumbing fixtures.' }],
};
class Statement {
  constructor(database, sql) { this.database = database; this.sql = sql; this.params = []; }
  bind(...params) { this.params = params; return this; }
  async first() {
    if (this.database.failRead) throw new Error('Simulated read outage');
    return this.database.sqlite.prepare(this.sql).get(...this.params) ?? null;
  }
  async all() { return { results: this.database.sqlite.prepare(this.sql).all(...this.params) }; }
  async run() {
    const result = this.database.sqlite.prepare(this.sql).run(...this.params);
    return { meta: { changes: Number(result.changes) } };
  }
}
export function environment() {
  const sqlite = new DatabaseSync(':memory:');
  sqlite.exec(readFileSync(new URL('../db/schema.sql', import.meta.url), 'utf8'));
  const DB = {
    sqlite, failRead: false, afterBatch: null, tail: Promise.resolve(),
    prepare(sql) { return new Statement(this, sql); },
    batch(statements) {
      const task = this.tail.then(async () => {
        sqlite.exec('BEGIN');
        try {
          const results = [];
          for (const statement of statements) results.push(await statement.run());
          sqlite.exec('COMMIT');
          if (this.afterBatch) await this.afterBatch();
          return results;
        } catch (error) {
          if (sqlite.isTransaction) sqlite.exec('ROLLBACK');
          throw error;
        }
      });
      this.tail = task.catch(() => {});
      return task;
    },
  };
  const objects = new Map();
  const BUCKET = {
    objects, failPut: false, corrupt: false,
    async put(key, content) {
      objects.set(key, new Uint8Array(content));
      if (this.failPut) throw new Error('Simulated ambiguous R2 write');
    },
    async get(key) {
      if (!objects.has(key)) return null;
      const bytes = new Uint8Array(objects.get(key));
      if (this.corrupt) bytes[0] ^= 1;
      return { arrayBuffer: async () => bytes.buffer };
    },
    async delete(key) { objects.delete(key); },
  };
  return { DB, BUCKET };
}
