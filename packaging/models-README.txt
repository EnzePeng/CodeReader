此目录需要放置至少一个 GGUF 模型文件。

默认配置使用 Qwen3.5-9B Q4_K_M：
https://huggingface.co/lmstudio-community/Qwen3.5-9B-GGUF/blob/main/Qwen3.5-9B-Q4_K_M.gguf

更轻量的 Qwen2.5-Coder-7B-Instruct Q4_K_M：
https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct-GGUF/tree/main

下载后把 .gguf 文件直接放在本目录，并将 config.json 中 llama.model
改为实际文件名。默认配置名为 Qwen3.5-9B.Q4_K_M.gguf；如果下载的文件名
是 Qwen3.5-9B-Q4_K_M.gguf，可以修改配置，也可以将文件重命名为默认名称。
