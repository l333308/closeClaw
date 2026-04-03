package main

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/sevan/closeclaw/internal/cache"
	"github.com/sevan/closeclaw/internal/config"
	"github.com/sevan/closeclaw/internal/pipeline"
	"github.com/sevan/closeclaw/internal/queue"
	"github.com/sevan/closeclaw/shared/schema"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	slog.SetDefault(logger)

	cfg, err := config.Load()
	if err != nil {
		slog.Error("config load failed", "err", err)
		os.Exit(1)
	}

	q, err := queue.New(cfg.RabbitMQ.URL)
	if err != nil {
		slog.Error("rabbitmq init failed", "err", err)
		os.Exit(1)
	}
	defer q.Close()

	r, err := cache.New(cfg.Redis.Addr, cfg.Redis.Password, cfg.Redis.DB)
	if err != nil {
		slog.Error("redis init failed", "err", err)
		os.Exit(1)
	}
	defer r.Close()

	eng := pipeline.New(q, r)

	srv := newHTTPServer(cfg.HTTP.Addr, eng, r)

	// 定时触发 crawl（每小时）
	go runScheduler(eng)

	// 优雅关闭
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		slog.Info("http server starting", "addr", cfg.HTTP.Addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			slog.Error("http server error", "err", err)
		}
	}()

	<-ctx.Done()
	slog.Info("shutting down")

	shutCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	srv.Shutdown(shutCtx)
}

// runScheduler 每小时触发一次热点抓取 pipeline
func runScheduler(eng *pipeline.Engine) {
	ticker := time.NewTicker(1 * time.Hour)
	defer ticker.Stop()

	// 启动时立即触发一次
	triggerJob(eng)

	for range ticker.C {
		triggerJob(eng)
	}
}

func triggerJob(eng *pipeline.Engine) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	job, err := eng.StartJob(ctx)
	if err != nil {
		slog.Error("start job failed", "err", err)
		return
	}
	slog.Info("job triggered", "job_id", job.ID)
}

// newHTTPServer 构建管理 HTTP 服务
func newHTTPServer(addr string, eng *pipeline.Engine, r *cache.Client) *http.Server {
	mux := http.NewServeMux()

	// POST /jobs/trigger — 手动触发 pipeline
	mux.HandleFunc("POST /jobs/trigger", func(w http.ResponseWriter, req *http.Request) {
		job, err := eng.StartJob(req.Context())
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{"job_id": job.ID})
	})

	// POST /jobs/{id}/advance — Agent 回调，推进阶段
	mux.HandleFunc("POST /jobs/{id}/advance", func(w http.ResponseWriter, req *http.Request) {
		jobID := req.PathValue("id")
		var updatedJob schema.Job
		if err := json.NewDecoder(req.Body).Decode(&updatedJob); err != nil {
			http.Error(w, "invalid body", http.StatusBadRequest)
			return
		}
		if err := eng.Advance(req.Context(), jobID, &updatedJob); err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	})

	// POST /jobs/{id}/fail — Agent 报告失败
	mux.HandleFunc("POST /jobs/{id}/fail", func(w http.ResponseWriter, req *http.Request) {
		jobID := req.PathValue("id")
		var body struct{ Reason string `json:"reason"` }
		json.NewDecoder(req.Body).Decode(&body)
		eng.FailJob(req.Context(), jobID, body.Reason)
		w.WriteHeader(http.StatusNoContent)
	})

	// GET /jobs — 列出所有 Job ID
	mux.HandleFunc("GET /jobs", func(w http.ResponseWriter, req *http.Request) {
		ids, err := r.ListJobs(req.Context())
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"jobs": ids, "count": len(ids)})
	})

	// GET /jobs/{id} — 查看单个 Job 状态
	mux.HandleFunc("GET /jobs/{id}", func(w http.ResponseWriter, req *http.Request) {
		jobID := req.PathValue("id")
		b, err := r.GetJob(req.Context(), jobID)
		if err != nil {
			http.Error(w, err.Error(), http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write(b)
	})

	// GET /healthz
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{"status":"ok"}`))
	})

	return &http.Server{
		Addr:         addr,
		Handler:      mux,
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 15 * time.Second,
	}
}
