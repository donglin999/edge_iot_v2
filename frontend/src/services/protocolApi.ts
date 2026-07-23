/**
 * Protocol registry API.
 *
 * The backend exposes per-protocol field schemas at /api/acquisition/protocols/.
 * The frontend uses these to render the device-add and device-edit forms
 * dynamically — no per-protocol UI code needed.
 */
import { apiClient, downloadFile } from './apiClient';

export type FieldKind = 'string' | 'int' | 'float' | 'bool' | 'enum' | 'secret';

export interface FieldSpec {
  name: string;
  label: string;
  kind: FieldKind;
  required: boolean;
  default: unknown;
  choices: Array<string | number> | null;
  help_text: string;
  example: unknown;
}

export interface ProtocolDescriptor {
  name: string;
  label: string;
  category: 'fieldbus' | 'industrial-ethernet' | 'iot' | 'opc' | 'other';
  description: string;
  supports_pause: boolean;
  identity_fields: string[];
  device_fields: FieldSpec[];
  point_fields: FieldSpec[];
}

export async function listProtocols(): Promise<ProtocolDescriptor[]> {
  const response = await apiClient.get<ProtocolDescriptor[]>('/acquisition/protocols/');
  return response.data;
}

export async function getProtocol(name: string): Promise<ProtocolDescriptor> {
  const response = await apiClient.get<ProtocolDescriptor>(`/acquisition/protocols/${name}/`);
  return response.data;
}

export async function downloadTemplate(protocols?: string[]): Promise<void> {
  await downloadFile(
    '/acquisition/protocols/template/',
    'edge_iot_excel_template.xlsx',
    protocols && protocols.length > 0 ? { protocols: protocols.join(',') } : undefined,
  );
}

/**
 * AntD Tag color for a protocol's category. Shared by every place that
 * renders a protocol badge (device list, device detail, ...) so they can't
 * drift into showing different colors for the same protocol.
 */
export function protocolTagColor(category?: ProtocolDescriptor['category']): string {
  switch (category) {
    case 'industrial-ethernet':
      return 'blue';
    case 'fieldbus':
      return 'orange';
    case 'iot':
      return 'purple';
    case 'opc':
      return 'cyan';
    default:
      return 'default';
  }
}
