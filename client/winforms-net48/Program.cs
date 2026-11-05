using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace TcpSessionTester
{
    internal static class Program
    {
        [STAThread]
        private static void Main()
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new MainForm());
        }
    }

    internal sealed class TargetPreset
    {
        public string Name;
        public string Host;
        public int Port;
        public TargetPreset(string name, string host, int port) { Name = name; Host = host; Port = port; }
        public override string ToString() { return Name; }
    }

    internal sealed class ProbeSnapshot
    {
        public int Attempted, Success, Failed, Active, Inflight, Timeout, Refused, Other;
    }

    internal sealed class ProbeResult
    {
        public string Host, StopReason;
        public int Port, Interval, Threads, MaxProbe, MaxFail, TimeoutSeconds;
        public ProbeSnapshot Stats;
        public double Elapsed;
    }

    internal sealed class TcpProber
    {
        private readonly object gate = new object();
        private readonly HashSet<Socket> openSockets = new HashSet<Socket>();
        private readonly ManualResetEvent stopEvent = new ManualResetEvent(false);
        private readonly string host;
        private readonly int port, intervalMs, threadCount, maxProbe, maxFail, timeoutMs;
        private int attempted, success, failed, consecutiveFailed, timeoutCount, refusedCount, otherCount;
        private DateTime startedAt;
        private volatile bool stopping;
        private string stopReason;
        public event Action<ProbeResult> Finished;

        public TcpProber(string host, int port, int intervalMs, int threadCount, int maxProbe, int maxFail, int timeoutSeconds)
        {
            this.host = host; this.port = port; this.intervalMs = intervalMs;
            this.threadCount = threadCount; this.maxProbe = maxProbe; this.maxFail = maxFail;
            timeoutMs = timeoutSeconds * 1000;
        }

        public ProbeSnapshot Snapshot()
        {
            lock (gate)
            {
                return new ProbeSnapshot {
                    Attempted = attempted, Success = success, Failed = failed,
                    Active = openSockets.Count,
                    Inflight = Math.Max(0, attempted - success - failed),
                    Timeout = timeoutCount, Refused = refusedCount, Other = otherCount
                };
            }
        }

        public void Start()
        {
            startedAt = DateTime.UtcNow;
            Task.Factory.StartNew((Action)(() =>
            {
                var workers = new Task[threadCount];
                for (int i = 0; i < workers.Length; i++) workers[i] = Task.Factory.StartNew(new Action(Worker));
                Task.WaitAll(workers);
                CloseAll();
                ProbeResult result;
                lock (gate)
                {
                    result = new ProbeResult {
                        Host = host, Port = port, Interval = intervalMs, Threads = threadCount,
                        MaxProbe = maxProbe, MaxFail = maxFail, TimeoutSeconds = timeoutMs / 1000,
                        StopReason = stopReason ?? "达到探测上限或已停止",
                        Stats = new ProbeSnapshot {
                            Attempted = attempted, Success = success, Failed = failed, Active = 0, Inflight = 0,
                            Timeout = timeoutCount, Refused = refusedCount, Other = otherCount
                        },
                        Elapsed = (DateTime.UtcNow - startedAt).TotalSeconds
                    };
                }
                var handler = Finished;
                if (handler != null) handler(result);
            }));
        }

        public int Stop()
        {
            lock (gate)
            {
                if (stopReason == null) stopReason = "手动停止";
            }
            SignalStop();
            return CloseAll();
        }

        private void SignalStop()
        {
            stopping = true;
            stopEvent.Set();
        }

        private int CloseAll()
        {
            Socket[] sockets;
            lock (gate) { sockets = openSockets.ToArray(); openSockets.Clear(); }
            foreach (Socket socket in sockets) CloseSocket(socket);
            return sockets.Length;
        }

        private static void CloseSocket(Socket socket)
        {
            try { socket.LingerState = new LingerOption(true, 0); } catch { }
            try { socket.Close(); } catch { }
        }

        private void Worker()
        {
            while (!stopping)
            {
                lock (gate)
                {
                    if (attempted >= maxProbe)
                    {
                        if (stopReason == null) stopReason = "达到探测上限 " + maxProbe;
                        SignalStop();
                        break;
                    }
                    if (consecutiveFailed >= maxFail)
                    {
                        if (stopReason == null) stopReason = "连续失败 " + maxFail + " 次";
                        SignalStop();
                        break;
                    }
                    attempted++;
                }

                Socket socket = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
                lock (gate) openSockets.Add(socket);
                Exception connectError = null;
                bool connected = false;
                using (var completed = new ManualResetEvent(false))
                {
                    try
                    {
                        socket.BeginConnect(host, port, ar =>
                        {
                            try { socket.EndConnect(ar); }
                            catch (Exception ex) { connectError = ex; }
                            finally { try { completed.Set(); } catch (ObjectDisposedException) { } }
                        }, null);
                        int which = WaitHandle.WaitAny(new WaitHandle[] { completed, stopEvent }, timeoutMs);
                        if (which == 0) connected = connectError == null && socket.Connected;
                        else if (which == WaitHandle.WaitTimeout) connectError = new SocketException((int)SocketError.TimedOut);
                        else connectError = new OperationCanceledException();
                    }
                    catch (Exception ex) { connectError = ex; }
                }

                if (connected && !stopping)
                {
                    lock (gate)
                    {
                        if (!stopping)
                        {
                            success++;
                            consecutiveFailed = 0;
                            // Socket remains in openSockets while the probe is active.
                        }
                        else connected = false;
                    }
                }

                if (!connected)
                {
                    lock (gate) openSockets.Remove(socket);
                    CloseSocket(socket);
                    if (stopping) break;
                    lock (gate)
                    {
                        failed++;
                        consecutiveFailed++;
                        var se = connectError as SocketException;
                        if (se != null && se.SocketErrorCode == SocketError.TimedOut)
                            timeoutCount++;
                        else if (se != null && se.SocketErrorCode == SocketError.ConnectionRefused)
                            refusedCount++;
                        else otherCount++;
                        if (consecutiveFailed >= maxFail)
                        {
                            if (stopReason == null) stopReason = "连续失败 " + maxFail + " 次";
                            SignalStop();
                        }
                    }
                }
                else if (intervalMs > 0 && stopEvent.WaitOne(intervalMs)) break;
            }
        }
    }

    internal sealed class MainForm : Form
    {
        private readonly ComboBox preset = new ComboBox();
        private readonly TextBox host = new TextBox(), port = new TextBox(), interval = new TextBox(),
            threads = new TextBox(), maxProbe = new TextBox(), maxFail = new TextBox(), timeout = new TextBox();
        private readonly Label successLabel = new Label(), failedLabel = new Label(), inflightLabel = new Label(), breakdown = new Label();
        private readonly TextBox log = new TextBox();
        private readonly ListView processList = new ListView();
        private readonly Button startButton = new Button(), stopButton = new Button();
        private readonly System.Windows.Forms.Timer refreshTimer = new System.Windows.Forms.Timer();
        private readonly TargetPreset[] presets = {
            new TargetPreset("阿里DNS", "223.6.6.6", 53),
            new TargetPreset("Google DNS", "8.8.8.8", 53),
            new TargetPreset("Cloudflare DNS", "1.1.1.1", 53),
            new TargetPreset("腾讯DNS", "119.29.29.29", 53),
            new TargetPreset("百度", "www.baidu.com", 80),
            new TargetPreset("我的服务器", "", 9000)
        };
        private TcpProber prober;
        private ProbeResult lastResult;
        private DateTime startTime;
        private bool usageBusy;

        public MainForm()
        {
            Text = "TCP连接数探测工具 · .NET Framework 4.8";
            MinimumSize = new Size(1050, 650);
            Size = new Size(1160, 720);
            StartPosition = FormStartPosition.CenterScreen;
            BuildUi();
            refreshTimer.Interval = 500;
            refreshTimer.Tick += (s, e) => RefreshUi();
            refreshTimer.Start();
            FormClosing += OnClosing;
            SelectPreset();
        }

        private void BuildUi()
        {
            var split = new SplitContainer { Dock = DockStyle.Fill, FixedPanel = FixedPanel.None };
            // Size it before setting minimums and the splitter distance. The
            // designer's default size is only 150x100 and is too small for
            // the two minimum pane widths.
            split.Size = ClientSize;
            split.Panel1MinSize = 650;
            split.Panel2MinSize = 320;
            split.SplitterDistance = 680;
            Controls.Add(split);
            var left = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(12), ColumnCount = 2, RowCount = 10 };
            left.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 180));
            left.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            split.Panel1.Controls.Add(left);

            preset.DropDownStyle = ComboBoxStyle.DropDownList;
            preset.Items.AddRange(presets);
            preset.SelectedIndexChanged += (s, e) => SelectPreset();
            AddRow(left, 0, "预设目标:", preset);

            host.Text = "";
            AddRow(left, 1, "服务器地址:", host);
            port.Text = "9000"; AddRow(left, 2, "端口:", port);
            preset.SelectedIndex = 0;
            interval.Text = "100"; AddRow(left, 3, "时间间隔 (ms):", interval);
            threads.Text = "10"; AddRow(left, 4, "线程数:", threads);
            maxProbe.Text = "10000"; AddRow(left, 5, "探测上限:", maxProbe);
            maxFail.Text = "100"; AddRow(left, 6, "连续失败几次停止:", maxFail);
            timeout.Text = "5"; AddRow(left, 7, "连接超时 (秒):", timeout);
            left.RowStyles.Add(new RowStyle(SizeType.Absolute, 48));
            left.RowStyles.Add(new RowStyle(SizeType.Percent, 100));

            var buttons = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true };
            startButton.Text = "开始探测"; startButton.Width = 105; startButton.Click += (s, e) => StartProbe();
            stopButton.Text = "停止探测"; stopButton.Width = 105; stopButton.Enabled = false; stopButton.Click += (s, e) => StopProbe();
            var clearButton = new Button { Text = "清空日志", Width = 105 };
            clearButton.Click += (s, e) => log.Clear();
            buttons.Controls.Add(startButton); buttons.Controls.Add(stopButton); buttons.Controls.Add(clearButton);
            left.Controls.Add(buttons, 0, 8); left.SetColumnSpan(buttons, 2);

            var output = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 2 };
            output.RowStyles.Add(new RowStyle(SizeType.Absolute, 88));
            output.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            var resultGroup = new GroupBox { Text = "探测结果", Dock = DockStyle.Fill };
            var stats = new FlowLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(8), WrapContents = true };
            successLabel.AutoSize = true; failedLabel.AutoSize = true; inflightLabel.AutoSize = true; breakdown.AutoSize = true;
            successLabel.Text = "成功连接数: 0"; failedLabel.Text = "失败连接数: 0"; inflightLabel.Text = "在途: 0";
            stats.Controls.Add(successLabel); stats.Controls.Add(failedLabel); stats.Controls.Add(inflightLabel); stats.Controls.Add(breakdown);
            resultGroup.Controls.Add(stats);
            var logGroup = new GroupBox { Text = "运行日志", Dock = DockStyle.Fill };
            log.Dock = DockStyle.Fill; log.Multiline = true; log.ReadOnly = true; log.ScrollBars = ScrollBars.Vertical;
            logGroup.Controls.Add(log);
            output.Controls.Add(resultGroup, 0, 0);
            output.Controls.Add(logGroup, 0, 1);
            left.Controls.Add(output, 0, 9); left.SetColumnSpan(output, 2);

            var usageGroup = new GroupBox { Text = "当前进程活动 TCP 连接（前 25）", Dock = DockStyle.Fill, Padding = new Padding(8) };
            processList.Dock = DockStyle.Fill; processList.View = View.Details; processList.FullRowSelect = true;
            processList.Columns.Add("PID", 55); processList.Columns.Add("进程名", 145); processList.Columns.Add("活动连接数", 100);
            usageGroup.Controls.Add(processList);
            split.Panel2.Controls.Add(usageGroup);
        }

        private static void AddRow(TableLayoutPanel panel, int row, string labelText, Control control)
        {
            panel.RowStyles.Add(new RowStyle(SizeType.Absolute, 42));
            var label = new Label { Text = labelText, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft };
            control.Dock = DockStyle.Fill;
            panel.Controls.Add(label, 0, row);
            panel.Controls.Add(control, 1, row);
        }

        private void SelectPreset()
        {
            var item = preset.SelectedItem as TargetPreset;
            if (item == null) return;
            if (!String.IsNullOrEmpty(item.Host)) host.Text = item.Host;
            port.Text = item.Port.ToString();
        }

        private void StartProbe()
        {
            int p, delay, workerCount, limit, failLimit, timeoutSeconds;
            if (String.IsNullOrWhiteSpace(host.Text) ||
                !Int32.TryParse(port.Text, out p) || p < 1 || p > 65535 ||
                !Int32.TryParse(interval.Text, out delay) || delay < 0 || delay > 60000 ||
                !Int32.TryParse(threads.Text, out workerCount) || workerCount < 1 || workerCount > 200 ||
                !Int32.TryParse(maxProbe.Text, out limit) || limit < 1 || limit > 1000000 ||
                !Int32.TryParse(maxFail.Text, out failLimit) || failLimit < 1 || failLimit > 1000000 ||
                !Int32.TryParse(timeout.Text, out timeoutSeconds) || timeoutSeconds < 1 || timeoutSeconds > 60)
            {
                MessageBox.Show("请检查输入：端口 1~65535，线程数 1~200，间隔 0~60000ms，探测上限和连续失败次数 1~1000000，超时 1~60 秒。",
                    "参数无效", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }

            lastResult = null;
            successLabel.Text = "成功连接数: 0"; failedLabel.Text = "失败连接数: 0";
            inflightLabel.Text = "在途: 0"; breakdown.Text = "";
            startTime = DateTime.Now;
            AppendLog("开始探测 " + host.Text.Trim() + ":" + p + "，线程 " + workerCount + "，间隔 " + delay + "ms");
            startButton.Enabled = false; stopButton.Enabled = true;
            prober = new TcpProber(host.Text.Trim(), p, delay, workerCount, limit, failLimit, timeoutSeconds);
            prober.Finished += OnFinished;
            prober.Start();
        }

        private void StopProbe()
        {
            if (prober == null) return;
            int released = prober.Stop();
            AppendLog("正在停止…已释放 " + released + " 个连接");
            stopButton.Enabled = false;
        }

        private void RefreshUi()
        {
            if (prober != null)
            {
                var stats = prober.Snapshot();
                successLabel.Text = "成功连接数: " + stats.Success;
                failedLabel.Text = "失败连接数: " + stats.Failed;
                inflightLabel.Text = "在途: " + stats.Inflight;
                breakdown.Text = "超时 " + stats.Timeout + " / 拒绝 " + stats.Refused + " / 其他 " + stats.Other;
            }
            if (usageBusy) return;
            usageBusy = true;
            Task.Run(new Func<List<ProcessRow>>(ReadProcessRows)).ContinueWith(task =>
            {
                if (IsDisposed || !IsHandleCreated) { usageBusy = false; return; }
                BeginInvoke((Action)(() =>
                {
                    usageBusy = false;
                    if (task.IsFaulted) return;
                    processList.BeginUpdate();
                    processList.Items.Clear();
                    foreach (var row in task.Result.Take(25))
                    {
                        var item = new ListViewItem(row.Pid.ToString());
                        item.SubItems.Add(row.Name);
                        item.SubItems.Add(row.Count.ToString());
                        if (row.Pid == Process.GetCurrentProcess().Id) item.BackColor = Color.LemonChiffon;
                        processList.Items.Add(item);
                    }
                    processList.EndUpdate();
                }));
            });
        }

        private void OnFinished(ProbeResult result)
        {
            BeginInvoke((Action)(() =>
            {
                lastResult = result;
                successLabel.Text = "成功连接数: " + result.Stats.Success;
                failedLabel.Text = "失败连接数: " + result.Stats.Failed;
                inflightLabel.Text = "在途: 0";
                AppendLog("探测结束，用时 " + result.Elapsed.ToString("0.0") + "s：成功 " +
                    result.Stats.Success + "，失败 " + result.Stats.Failed + "，共尝试 " + result.Stats.Attempted);
                AppendLog("失败分类：超时 " + result.Stats.Timeout + " / 拒绝 " +
                    result.Stats.Refused + " / 其他 " + result.Stats.Other);
                AppendLog("停止原因：" + result.StopReason);
                AppendLog("结论：目标最大并发连接数约 " + result.Stats.Success);
                AppendLog("连接已全部释放");
                string path = ExportCsv(result);
                if (path != null) AppendLog("结果已导出: " + path);
                prober = null;
                startButton.Enabled = true; stopButton.Enabled = false;
            }));
        }

        private string ExportCsv(ProbeResult r)
        {
            try
            {
                string baseDir = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
                if (String.IsNullOrEmpty(baseDir) || !Directory.Exists(baseDir))
                    baseDir = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
                string path = Path.Combine(baseDir, "tcp_probe_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".csv");
                string[] rows = {
                    Csv("目标", r.Host + ":" + r.Port), Csv("时间间隔(ms)", r.Interval.ToString()),
                    Csv("线程数", r.Threads.ToString()), Csv("探测上限", r.MaxProbe.ToString()),
                    Csv("连续失败停止", r.MaxFail.ToString()), Csv("连接超时(秒)", r.TimeoutSeconds.ToString()),
                    Csv("成功", r.Stats.Success.ToString()), Csv("失败", r.Stats.Failed.ToString()),
                    Csv("超时失败", r.Stats.Timeout.ToString()), Csv("拒绝失败", r.Stats.Refused.ToString()),
                    Csv("其他失败", r.Stats.Other.ToString()), Csv("共尝试", r.Stats.Attempted.ToString()),
                    Csv("用时(秒)", r.Elapsed.ToString("0.0"))
                };
                File.WriteAllLines(path, rows, new UTF8Encoding(true));
                return path;
            }
            catch { return null; }
        }

        private static string Csv(string key, string value)
        {
            return "\"" + key.Replace("\"", "\"\"") + "\",\"" + value.Replace("\"", "\"\"") + "\"";
        }

        private void AppendLog(string message)
        {
            log.AppendText("[" + DateTime.Now.ToString("HH:mm:ss") + "] " + message + Environment.NewLine);
        }

        private void OnClosing(object sender, FormClosingEventArgs e)
        {
            refreshTimer.Stop();
            if (prober != null) prober.Stop();
        }

        private sealed class ProcessRow
        {
            public uint Pid;
            public string Name;
            public int Count;
        }

        [DllImport("iphlpapi.dll", SetLastError = true)]
        private static extern uint GetExtendedTcpTable(IntPtr table, ref uint size, bool order, int addressFamily, int tableClass, uint reserved);

        private static List<ProcessRow> ReadProcessRows()
        {
            uint size = 0;
            GetExtendedTcpTable(IntPtr.Zero, ref size, true, 2, 5, 0);
            if (size == 0) return new List<ProcessRow>();
            IntPtr buffer = Marshal.AllocHGlobal((int)size);
            try
            {
                uint status = GetExtendedTcpTable(buffer, ref size, true, 2, 5, 0);
                if (status != 0) return new List<ProcessRow>();
                int count = Marshal.ReadInt32(buffer);
                int rowSize = Marshal.SizeOf(typeof(TcpRowOwnerPid));
                var ports = new Dictionary<uint, HashSet<uint>>();
                for (int i = 0; i < count; i++)
                {
                    var row = (TcpRowOwnerPid)Marshal.PtrToStructure(
                        IntPtr.Add(buffer, 4 + i * rowSize), typeof(TcpRowOwnerPid));
                    if (!IsActiveState(row.State)) continue;
                    HashSet<uint> set;
                    if (!ports.TryGetValue(row.Pid, out set)) ports[row.Pid] = set = new HashSet<uint>();
                    if (row.State != 2 && row.State != 11) set.Add(row.LocalPort);
                }
                var result = new List<ProcessRow>();
                foreach (var pair in ports)
                {
                    if (pair.Value.Count == 0) continue;
                    string name = "未知";
                    try { name = Process.GetProcessById((int)pair.Key).ProcessName; } catch { }
                    result.Add(new ProcessRow { Pid = pair.Key, Name = name, Count = pair.Value.Count });
                }
                return result.OrderByDescending(x => x.Count).ToList();
            }
            finally { Marshal.FreeHGlobal(buffer); }
        }

        private static bool IsActiveState(uint state)
        {
            return state == 0 || state == 2 || state == 3 || state == 4 || state == 5 || state == 6 ||
                state == 7 || state == 8 || state == 9 || state == 10 || state == 11;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct TcpRowOwnerPid
        {
            public uint State, LocalAddress, LocalPort, RemoteAddress, RemotePort, Pid;
        }
    }
}
