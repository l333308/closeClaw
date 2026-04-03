package schema

import "time"

// Stage 表示 pipeline 中的执行阶段
type Stage string

const (
	StageCrawl   Stage = "crawl"
	StageDedup   Stage = "dedup"
	StageAnalyze Stage = "analyze"
	StageWrite   Stage = "write"
	StageVideo   Stage = "video"
	StagePublish Stage = "publish"
)

// Status 表示 Job 或阶段的执行状态
type Status string

const (
	StatusPending    Status = "pending"
	StatusRunning    Status = "running"
	StatusDone       Status = "done"
	StatusFailed     Status = "failed"
	StatusSkipped    Status = "skipped"
)

// HotTopic 是抓取阶段产出的热点条目
type HotTopic struct {
	ID        string    `json:"id"`
	Source    string    `json:"source"`    // "twitter" | "reddit"
	Title     string    `json:"title"`
	URL       string    `json:"url"`
	Content   string    `json:"content"`
	Score     float64   `json:"score"`
	CreatedAt time.Time `json:"created_at"`
}

// AnalysisResult 是分析阶段产出
type AnalysisResult struct {
	Summary    string   `json:"summary"`
	Keywords   []string `json:"keywords"`
	Sentiment  string   `json:"sentiment"`
	Relevance  float64  `json:"relevance"`
}

// CopyResult 是文案生成阶段产出
type CopyResult struct {
	Title      string `json:"title"`
	Script     string `json:"script"`
	Hashtags   []string `json:"hashtags"`
}

// VideoResult 是视频生成阶段产出
type VideoResult struct {
	FilePath   string `json:"file_path"`
	Duration   int    `json:"duration_sec"`
	ThumbnailPath string `json:"thumbnail_path"`
}

// Job 是贯穿整个 pipeline 的核心数据结构
type Job struct {
	ID        string    `json:"id"`
	Stage     Stage     `json:"stage"`
	Status    Status    `json:"status"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
	Error     string    `json:"error,omitempty"`

	Topic    *HotTopic       `json:"topic,omitempty"`
	Analysis *AnalysisResult `json:"analysis,omitempty"`
	Copy     *CopyResult     `json:"copy,omitempty"`
	Video    *VideoResult    `json:"video,omitempty"`
}

// StageMessage 是各阶段间通过 RabbitMQ 传递的消息体
type StageMessage struct {
	JobID   string `json:"job_id"`
	Stage   Stage  `json:"stage"`
	Payload []byte `json:"payload"`
}
