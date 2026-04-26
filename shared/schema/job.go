package schema

import "time"

// Stage 表示 pipeline 中的执行阶段
type Stage string

const (
	StageCrawl   Stage = "crawl"
	StageDedup   Stage = "dedup"
	StageAnalyze Stage = "analyze"
	StageWrite   Stage = "write"
	StageReview  Stage = "review"
	StageVideo   Stage = "video"
	StagePublish Stage = "publish"
)

// Status 表示 Job 或阶段的执行状态
type Status string

const (
	StatusPending Status = "pending"
	StatusRunning Status = "running"
	StatusDone    Status = "done"
	StatusFailed  Status = "failed"
	StatusSkipped Status = "skipped"
)

// HotTopic 是抓取阶段产出的热点条目
type HotTopic struct {
	ID        string    `json:"id"`
	Source    string    `json:"source"` // "twitter" | "reddit"
	Title     string    `json:"title"`
	URL       string    `json:"url"`
	Content   string    `json:"content"`
	Score     float64   `json:"score"`
	CreatedAt time.Time `json:"created_at"`
}

// AnalysisResult 是分析阶段产出
type AnalysisResult struct {
	Summary        string   `json:"summary"`
	Keywords       []string `json:"keywords"`
	Sentiment      string   `json:"sentiment"`
	Relevance      float64  `json:"relevance"`
	CorePoint      string   `json:"core_point,omitempty"`
	WhyItMatters   string   `json:"why_it_matters,omitempty"`
	ImpactOnPeople string   `json:"impact_on_people,omitempty"`
	StanceHint     string   `json:"stance_hint,omitempty"`
}

// CopyResult 是文案生成阶段产出
type CopyResult struct {
	Title    string   `json:"title"`
	Script   string   `json:"script"`
	Hashtags []string `json:"hashtags"`
}

// CopyReviewResult 是文案评分阶段产出
type CopyReviewResult struct {
	Attraction         int      `json:"attraction"`
	Emotion            int      `json:"emotion"`
	InformationDensity int      `json:"information_density"`
	Virality           int      `json:"virality"`
	Verdict            string   `json:"verdict"`
	Summary            string   `json:"summary,omitempty"`
	Suggestions        []string `json:"suggestions,omitempty"`
}

// VideoResult 是视频生成阶段产出
type VideoResult struct {
	FilePath      string `json:"file_path"`
	Duration      int    `json:"duration_sec"`
	ThumbnailPath string `json:"thumbnail_path"`
}

// Job 是贯穿整个 pipeline 的核心数据结构
type Job struct {
	ID               string    `json:"id"`
	TriggerJobID     string    `json:"trigger_job_id,omitempty"`
	Stage            Stage     `json:"stage"`
	Status           Status    `json:"status"`
	CreatedAt        time.Time `json:"created_at"`
	UpdatedAt        time.Time `json:"updated_at"`
	Error            string    `json:"error,omitempty"`
	CopyRewriteCount int       `json:"copy_rewrite_count,omitempty"`
	BatchTopicCount  int       `json:"batch_topic_count,omitempty"`
	VideoRankScore   float64   `json:"video_rank_score,omitempty"`
	VideoPickStatus  string    `json:"video_pick_status,omitempty"`

	Topic    *HotTopic         `json:"topic,omitempty"`
	Analysis *AnalysisResult   `json:"analysis,omitempty"`
	Copy     *CopyResult       `json:"copy,omitempty"`
	Review   *CopyReviewResult `json:"review,omitempty"`
	Video    *VideoResult      `json:"video,omitempty"`
}

// StageMessage 是各阶段间通过 RabbitMQ 传递的消息体
type StageMessage struct {
	JobID   string `json:"job_id"`
	Stage   Stage  `json:"stage"`
	Payload []byte `json:"payload"`
}
