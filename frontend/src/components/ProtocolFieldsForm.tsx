/**
 * Renders form items for a list of FieldSpec descriptors.
 *
 * Used for both device-level fields and point-level fields. Each FieldSpec
 * declares its kind ("string"/"int"/"float"/"bool"/"enum"/"secret"), required,
 * default, choices, etc., and we map that to the matching AntD input.
 */
import React from 'react';
import { Form, Input, InputNumber, Select, Switch, Tooltip } from 'antd';
import { QuestionCircleOutlined } from '@ant-design/icons';
import type { FieldSpec } from '../services/protocolApi';

interface Props {
  fields: FieldSpec[];
  /** Optional prefix for nested form values (e.g. ['metadata']). */
  namePath?: (string | number)[];
}

const ProtocolFieldsForm: React.FC<Props> = ({ fields, namePath }) => {
  if (!fields || fields.length === 0) {
    return null;
  }

  return (
    <>
      {fields.map((spec) => {
        const fullName = namePath ? [...namePath, spec.name] : spec.name;
        const label = (
          <span>
            {spec.label}
            {spec.help_text && (
              <Tooltip title={spec.help_text}>
                {' '}
                <QuestionCircleOutlined style={{ color: '#999' }} />
              </Tooltip>
            )}
          </span>
        );
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const rules: any[] = [];
        if (spec.required) {
          rules.push({ required: true, message: `请填写 ${spec.label}` });
        }

        let control: React.ReactNode;
        switch (spec.kind) {
          case 'int':
          case 'float':
            control = (
              <InputNumber
                style={{ width: '100%' }}
                step={spec.kind === 'int' ? 1 : 0.1}
                precision={spec.kind === 'int' ? 0 : undefined}
                placeholder={spec.example != null ? String(spec.example) : ''}
              />
            );
            break;
          case 'bool':
            control = <Switch />;
            break;
          case 'enum':
            control = (
              <Select
                placeholder="请选择"
                allowClear={!spec.required}
                options={(spec.choices ?? []).map((c) => ({
                  label: String(c),
                  value: c as string | number,
                }))}
              />
            );
            break;
          case 'secret':
            control = <Input.Password placeholder="••••••" autoComplete="new-password" />;
            break;
          case 'string':
          default:
            control = (
              <Input placeholder={spec.example != null ? String(spec.example) : ''} />
            );
        }

        return (
          <Form.Item
            key={spec.name}
            name={fullName}
            label={label}
            rules={rules}
            initialValue={spec.default ?? undefined}
            valuePropName={spec.kind === 'bool' ? 'checked' : 'value'}
          >
            {control}
          </Form.Item>
        );
      })}
    </>
  );
};

export default ProtocolFieldsForm;
