/**
 * v2 协议 Excel 导入弹窗(设备管理页「导入配置」按钮)。
 *
 * 契约见 docs/excel-import-export-v2.md:一个工作簿 = 一个协议,「设备」+「测点」
 * 两个 sheet,协议由后端自动识别(不用先选协议)。同步导入、整体一个事务 ——
 * 任何行级错误都不写任何数据,所以失败时界面上什么都不用回滚,只需要把错误
 * 逐行列出来让用户去改源文件(样式沿 protocols/scada/ScadaConfig.tsx 的错误
 * Alert)。
 *
 * legacy 40 列大宽表文件传进来会被后端拒(不是 v2 两表格式);命中时不逐行列
 * 「缺 sheet」这类没意义的错误,而是提示改去「导入作业」页——那条旧流程原样
 * 保留,存量文件还能导。
 */
import { useState } from 'react';
import { Alert, Button, Modal, Space, Typography, Upload, message } from 'antd';
import { InboxOutlined } from '@ant-design/icons';
import type { UploadProps } from 'antd';
import { Link } from 'react-router-dom';

import {
  extractProtocolImportErrors,
  importProtocolWorkbook,
  isLegacyFormatError,
  type ProtocolImportResponse,
} from '../services/protocolApi';

const { Text, Paragraph } = Typography;
const { Dragger } = Upload;

interface ProtocolExcelImportModalProps {
  open: boolean;
  onClose: () => void;
  /** 导入成功后通知宿主刷新设备列表。 */
  onImported?: () => void;
}

const ProtocolExcelImportModal = ({ open, onClose, onImported }: ProtocolExcelImportModalProps) => {
  const [importing, setImporting] = useState(false);
  const [result, setResult] = useState<ProtocolImportResponse | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [legacyHint, setLegacyHint] = useState(false);

  const reset = () => {
    setResult(null);
    setErrors([]);
    setLegacyHint(false);
  };

  const handleClose = () => {
    reset();
    onClose();
  };

  /** antd Upload 的 beforeUpload:返回 false 阻止它自己发请求,由我们接管。 */
  const handleUpload: UploadProps['beforeUpload'] = async (file) => {
    setImporting(true);
    reset();
    try {
      const response = await importProtocolWorkbook(file as File);
      setResult(response);
      message.success(
        `导入完成(${response.protocol})—— 新建 ${response.created.devices} 台设备 / ` +
          `${response.created.points} 个测点 / ${response.created.tasks} 个任务`,
      );
      onImported?.();
    } catch (error) {
      setResult(null);
      if (isLegacyFormatError(error)) {
        setLegacyHint(true);
      } else {
        setErrors(extractProtocolImportErrors(error));
      }
    } finally {
      setImporting(false);
    }
    return false;
  };

  return (
    <Modal
      title="导入配置(Excel)"
      open={open}
      onCancel={handleClose}
      footer={<Button onClick={handleClose}>关闭</Button>}
      destroyOnHidden
    >
      <Paragraph type="secondary">
        上传按协议分表的 Excel 工作簿(「设备」+「测点」两个 sheet)——协议由文件内容自动识别,
        不用先选协议。整份文件一个事务:任何一行有问题都不会写入任何数据。
      </Paragraph>

      <Dragger accept=".xlsx" showUploadList={false} beforeUpload={handleUpload} disabled={importing}>
        <p className="ant-upload-drag-icon">
          <InboxOutlined />
        </p>
        <p className="ant-upload-text">点击或拖拽 .xlsx 文件到此处上传</p>
        <p className="ant-upload-hint">
          SCADA 设备请在「添加设备 → 选 scada」或编辑 scada 设备的弹窗里导入(网关两表);旧版 40 列通用模板请到「导入作业」页。
        </p>
      </Dragger>

      {importing && (
        <Text type="secondary" style={{ display: 'block', marginTop: 12 }}>
          导入中…
        </Text>
      )}

      {legacyHint && (
        <Alert
          style={{ marginTop: 16 }}
          type="warning"
          showIcon
          message="这份文件像是旧版 40 列通用模板,不是新版两表格式"
          description={
            <Space direction="vertical" size={4}>
              <Text>
                新版模板按协议拆成「设备」「测点」两个 sheet;旧版单表文件请改到「导入作业」页导入
                (旧流程原样保留,存量文件仍可用)。
              </Text>
              <Link to="/import" onClick={handleClose}>
                前往「导入作业」页 →
              </Link>
            </Space>
          }
        />
      )}

      {!legacyHint && errors.length > 0 && (
        <Alert
          style={{ marginTop: 16 }}
          type="error"
          showIcon
          message="导入失败 —— 未写入任何数据"
          description={
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {errors.map((e) => (
                <li key={e}>{e}</li>
              ))}
            </ul>
          }
        />
      )}

      {result && (
        <Alert
          style={{ marginTop: 16 }}
          type="success"
          showIcon
          message={`导入成功 —— ${result.protocol}`}
          description={
            <Text>
              新建:{result.created.devices} 台设备 / {result.created.points} 个测点 /{' '}
              {result.created.tasks} 个任务
              <br />
              更新:{result.updated.devices} 台设备 / {result.updated.points} 个测点 /{' '}
              {result.updated.tasks} 个任务
            </Text>
          }
        />
      )}
    </Modal>
  );
};

export default ProtocolExcelImportModal;
