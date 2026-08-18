from flask import Flask, request, jsonify, render_template_string, redirect, url_for
import time
import uuid

app = Flask(__name__)

# ===============================
# 存储任务和结果
# ===============================
tasks = {}       # { "machine": {task_json} }
results = []     # [{ "task_id":..., "machine": ..., "status": ..., "message": ..., "time": ..., "progress": ... }]

# 机器在线状态动态收集
agents_status = {}  # { "机器名": {"last_seen": 时间戳} }

# ===============================
# Agent 拉取任务接口
# ===============================
@app.route("/task", methods=["GET"])
def get_task():
    machine = request.args.get("machine")
    if not machine:
        return jsonify({})

    # 新机器自动注册
    if machine not in agents_status:
        agents_status[machine] = {"last_seen": 0}

    # 更新时间戳
    agents_status[machine]["last_seen"] = time.time()

    # 返回任务
    task = tasks.pop(machine, None)
    return jsonify(task if task else {})

# ===============================
# Agent 回传结果接口
# ===============================
@app.route("/report", methods=["POST"])
def report_result():
    data = request.get_json(force=True)
    data["time"] = time.strftime("%Y-%m-%d %H:%M:%S")

    task_id = data.get("task_id")
    if not task_id:
        return jsonify({"status": "error", "msg": "缺少 task_id"})

    # 更新已有记录
    updated = False
    for r in results:
        if r.get("task_id") == task_id:
            r.update(data)
            updated = True
            break
    if not updated:
        # 防止没有占位记录时也能创建一条
        results.append(data)

    print(f"[REPORT] {data}")
    return jsonify({"status": "received"})

# ===============================
# 任务结果 JSON 接口（前端轮询）
# ===============================
@app.route("/results_json")
def results_json():
    return jsonify(results)

# ===============================
# Web 界面首页
# ===============================
@app.route("/", methods=["GET", "POST"])
def dashboard():
    if request.method == "POST":
        machines = request.form.getlist("machines")
        action = request.form.get("action")
        source = request.form.get("source")
        destination = request.form.get("destination")
        mode = request.form.get("mode")

        for m in machines:
            task_id = str(uuid.uuid4())
            task_data = {
                "task_id": task_id,
                "machines": [m],
                "action": action,
                "source": source,
                "destination": destination,
                "mode": mode
            }
            tasks[m] = task_data

            # 下发任务时立即插入占位记录（只插入一次）
            results.append({
                "task_id": task_id,
                "machine": m,
                "status": "progress",
                "message": "任务已下发，等待执行...",
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "progress": 0
            })

        return redirect(url_for("dashboard"))

    # GET 请求，渲染表格和任务下发表单
    html_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8"/>
        <title>工厂控制端 - Dashboard</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; }
            h2 { color: #333; }
            table { border-collapse: collapse; width: 100%; margin-top: 15px; }
            th, td { border: 1px solid #ccc; padding: 8px; text-align: center; }
            th { background-color: #f2f2f2; }
            tr:nth-child(even) { background-color: #f9f9f9; }
            .ok { color: green; font-weight: bold; }
            .fail { color: red; font-weight: bold; }
            .progress { color: orange; font-weight: bold; }
            fieldset { margin-top: 20px; padding: 10px; }
        </style>
    </head>
    <body>
        <h2>工厂控制端 - Dashboard</h2>

        <fieldset>
            <legend>下发任务</legend>
            <form method="post">
                <label>选择机器:</label><br/>
                {% for m in agents_status.keys() %}
                    <input type="checkbox" name="machines" value="{{ m }}">
                    {{ m }} ({{ '在线' if (time.time() - agents_status[m]['last_seen']) < 20 else '离线' }})<br/>
                {% endfor %}<br/>

                <label>任务类型:</label>
                <select name="action">
                    <option value="deploy_folder">复制文件夹</option>
                    <option value="run_command">执行命令</option>
                </select><br/><br/>

                <label>源路径 (共享路径或命令):</label>
                <input type="text" name="source" style="width:300px;" placeholder="\\\\192.168.36.248\\test\\ATA"><br/><br/>

                <label>目标路径:</label>
                <input type="text" name="destination" style="width:300px;" placeholder="D:\\TE"><br/><br/>

                <label>模式:</label>
                <select name="mode">
                    <option value="overwrite">覆盖</option>
                    <option value="mirror">镜像</option>
                </select><br/><br/>

                <button type="submit">下发任务</button>
            </form>
        </fieldset>

        <h3>执行日志（自动刷新）</h3>
        <table>
            <thead>
            <tr>
                <th>时间</th>
                <th>机器</th>
                <th>状态</th>
                <th>进度</th>
                <th>信息</th>
            </tr>
            </thead>
            <tbody id="results_body">
                {% for r in results %}
                <tr>
                    <td>{{ r.time }}</td>
                    <td>{{ r.machine }}</td>
                    <td class="{{ 'ok' if r.status=='success' else ('fail' if r.status=='error' else 'progress') }}">{{ r.status }}</td>
                    <td>{{ r.progress if r.progress is defined else '' }}</td>
                    <td>{{ r.message }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>

        <script>
        function fetchResults() {
            fetch("/results_json")
                .then(res => res.json())
                .then(data => {
                    const tbody = document.querySelector("#results_body");
                    tbody.innerHTML = "";
                    data.forEach(r => {
                        const tr = document.createElement("tr");
                        const statusClass = r.status=='success'?'ok':(r.status=='error'?'fail':'progress');
                        const progressText = (r.status=='progress' && r.progress!=undefined) ? r.progress+'%' : '';
                        tr.innerHTML = `
                            <td>${r.time}</td>
                            <td>${r.machine}</td>
                            <td class="${statusClass}">${r.status}</td>
                            <td>${progressText}</td>
                            <td>${r.message}</td>
                        `;
                        tbody.appendChild(tr);
                    });
                });
        }
        setInterval(fetchResults, 2000);
        window.onload = fetchResults;
        </script>
    </body>
    </html>
    """
    return render_template_string(html_template, results=results, agents_status=agents_status, time=time)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
