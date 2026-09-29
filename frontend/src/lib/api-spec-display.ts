type ApiRecord = Record<string, unknown>;

export interface ApiResponseDisplay {
  status: string;
  outputType: string | null;
}

function record(value: unknown): ApiRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as ApiRecord)
    : {};
}

function schemaType(value: unknown): string | null {
  const schema = record(value);
  const reference = String(schema.$ref ?? '').trim();
  if (reference) return reference.split('/').at(-1) ?? null;
  if (schema.type === 'array') {
    const itemType = schemaType(schema.items);
    return itemType ? `${itemType}[]` : 'array';
  }
  return typeof schema.type === 'string' && schema.type.trim() ? schema.type : null;
}

function contentSchema(value: unknown): string | null {
  const content = record(value);
  for (const media of Object.values(content)) {
    const type = schemaType(record(media).schema);
    if (type) return type;
  }
  return null;
}

/** Read-only display helpers for the OpenAPI document already stored as api_spec. */
export function apiOperationInputTypes(operation: unknown): string[] {
  const source = record(operation);
  const inputs: string[] = [];
  const bodyType = contentSchema(record(source.requestBody).content);
  if (bodyType) inputs.push(bodyType);
  for (const rawParameter of Array.isArray(source.parameters) ? source.parameters : []) {
    const parameter = record(rawParameter);
    const name = String(parameter.name ?? '').trim();
    const type = schemaType(parameter.schema);
    if (name && type) inputs.push(`${name}: ${type}`);
  }
  return inputs;
}

export function apiOperationResponses(operation: unknown): ApiResponseDisplay[] {
  return Object.entries(record(record(operation).responses))
    .map(([status, response]) => ({ status, outputType: contentSchema(record(response).content) }))
    .sort((left, right) => left.status.localeCompare(right.status, undefined, { numeric: true }));
}
