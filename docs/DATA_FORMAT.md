# 数据导入格式

数据根目录包含病例子目录，每例四个三维 NIfTI 文件，角色为 `w`、`f`、`in`、`opp`。导入器按 NIfTI 相对路径的父目录组合四序列；四角色齐全且文件存在才创建任务。

```text
data/DEMO001/{W,F,In,Opp}.nii.gz
manifests/patient_name_zh_patient_id_mapping.csv
manifests/conversion_manifest.csv
manifests/source_quartet_manifest.csv
```

可直接参考仓库中的 [示例清单](../examples/demo/manifests)。路径必须相对于 `DATA_ROOT`，不可使用绝对路径或 `..`。

## 清单

- `patient_name_zh_patient_id_mapping.csv`：`PatientID`、`中文姓名` 或 `显示姓名`、`映射状态`、`需人工复核`；可选 `复核原因`、`源身份状态`。公开示例仅填写演示身份。
- `conversion_manifest.csv`：`patient_id`、`normalized_family`、`target_role`、`nifti_relative_path`、`status`（须为 `converted`）。运行 `app.verify_dataset` 时，还需 `json_relative_path`；`nifti_bytes`、`json_bytes` 可用于大小校验。
- `source_quartet_manifest.csv`（可选）：`output_acquisition_directory`、`study_dates`、`normalized_family`。

准备好文件后调整 `.env` 并重启 API，或使用管理员页面中的「同步新病例」。导入幂等，同一来源目录不会重复创建病例。需身份核对的记录会隔离在常规任务队列之外。

## 胰腺参考掩膜

`PANCREAS_PREDICTION_ROOT` 下每个子目录包含二值掩膜和 `inference_metadata.json`。所需字段：

```json
{
  "patient_id": "DEMO001",
  "status": "completed",
  "native_shape": [320, 260, 42],
  "native_affine": [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]],
  "prediction_filename": "pancreas_mask.nii.gz",
  "model_method_id": "your-method-or-manual-reference"
}
```

这里的仿射矩阵仅说明字段形状，实际必须使用对应 NIfTI 的真实矩阵。平台会校验 PatientID、尺寸和仿射矩阵；只有唯一匹配的参考结果才进入 PDFF 项目。参考掩膜不写入 ROI 标注。

病例、标注和备份属于部署数据，不应批量提交到源码仓库。
