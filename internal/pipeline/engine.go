// Package pipeline 实现 DAG 状态机，驱动各阶段有序/并行执行。
//
// Pipeline 拓扑：
//
//	crawl → dedup → [analyze ∥ write] → video → publish
//
// analyze 与 write 并行执行（write 依赖 analyze 结果时串行，
// 当前 MVP 实现为串行以保持简单，后续可拆分并行 goroutine）。
package pipeline

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"github.com/google/uuid"
	"github.com/sevan/closeclaw/internal/cache"
	"github.com/sevan/closeclaw/internal/queue"
	"github.com/sevan/closeclaw/shared/schema"
)

// Engine 负责创建 Job 并推送到第一个队列，驱动 pipeline 启动
type Engine struct {
	q   *queue.Client
	r   *cache.Client
}

// New 创建 Engine
func New(q *queue.Client, r *cache.Client) *Engine {
	return &Engine{q: q, r: r}
}

// StartJob 创建一个新 Job 并推送到 crawl 队列
func (e *Engine) StartJob(ctx context.Context) (*schema.Job, error) {
	job := &schema.Job{
		ID:        uuid.New().String(),
		Stage:     schema.StageCrawl,
		Status:    schema.StatusPending,
		CreatedAt: time.Now().UTC(),
		UpdatedAt: time.Now().UTC(),
	}

	if err := e.r.SaveJob(ctx, job); err != nil {
		return nil, fmt.Errorf("save job: %w", err)
	}

	msg := &schema.StageMessage{
		JobID: job.ID,
		Stage: schema.StageCrawl,
	}

	payload, err := json.Marshal(msg)
	if err != nil {
		return nil, fmt.Errorf("marshal stage message: %w", err)
	}

	if err := e.q.Publish(ctx, queue.QueueCrawl, payload); err != nil {
		return nil, fmt.Errorf("publish to crawl queue: %w", err)
	}

	slog.Info("pipeline started", "job_id", job.ID)
	return job, nil
}

// Advance 将 Job 推进到下一阶段
// 由各 Python Agent 完成本阶段后，通过 HTTP 回调触发
func (e *Engine) Advance(ctx context.Context, jobID string, updatedJob *schema.Job) error {
	nextQueue, ok := nextStageQueue(updatedJob.Stage)
	if !ok {
		slog.Info("pipeline completed", "job_id", jobID)
		return e.r.SetStage(ctx, jobID, string(updatedJob.Stage), string(schema.StatusDone))
	}

	// 持久化最新 Job 状态
	updatedJob.UpdatedAt = time.Now().UTC()
	updatedJob.Status = schema.StatusDone
	if err := e.r.SaveJob(ctx, updatedJob); err != nil {
		return fmt.Errorf("save advanced job: %w", err)
	}

	// 推送下一阶段消息
	b, err := json.Marshal(updatedJob)
	if err != nil {
		return fmt.Errorf("marshal job: %w", err)
	}

	msg := &schema.StageMessage{
		JobID:   jobID,
		Stage:   nextStage(updatedJob.Stage),
		Payload: b,
	}

	msgBytes, err := json.Marshal(msg)
	if err != nil {
		return fmt.Errorf("marshal stage msg: %w", err)
	}

	if err := e.q.Publish(ctx, nextQueue, msgBytes); err != nil {
		return fmt.Errorf("publish to %s: %w", nextQueue, err)
	}

	slog.Info("stage advanced", "job_id", jobID, "next_stage", nextStage(updatedJob.Stage))
	return nil
}

// FailJob 将 Job 标记为失败
func (e *Engine) FailJob(ctx context.Context, jobID, reason string) error {
	return e.r.SetStage(ctx, jobID, "failed", string(schema.StatusFailed))
}

// nextStageQueue 返回当前阶段完成后应推送到哪个队列
func nextStageQueue(current schema.Stage) (string, bool) {
	order := []struct {
		stage Stage
		q     string
	}{
		{schema.StageCrawl, queue.QueueDedup},
		{schema.StageDedup, queue.QueueAnalyze},
		{schema.StageAnalyze, queue.QueueWrite},
		{schema.StageWrite, queue.QueueVideo},
		{schema.StageVideo, queue.QueuePublish},
	}
	for _, s := range order {
		if s.stage == current {
			return s.q, true
		}
	}
	return "", false
}

// nextStage 返回下一个阶段枚举值
func nextStage(current schema.Stage) schema.Stage {
	order := []schema.Stage{
		schema.StageCrawl,
		schema.StageDedup,
		schema.StageAnalyze,
		schema.StageWrite,
		schema.StageVideo,
		schema.StagePublish,
	}
	for i, s := range order {
		if s == current && i+1 < len(order) {
			return order[i+1]
		}
	}
	return current
}

// type alias 避免 import cycle
type Stage = schema.Stage
