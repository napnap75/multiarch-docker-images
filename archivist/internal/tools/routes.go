// Routes.go binds the tools-page application routes: the tool list, the
// run trigger, the live job state and the cancel action. Superuser-only,
// like every /api/archivist route.
package tools

import (
	"ganigeorgiev.com/pocketbase/pocketbase/core"
)

// BindRoutes registers the tools routes on the PocketBase router.
func BindRoutes(app core.App, s3 S3Store) {
	app.OnServe().BindFunc(func(e *core.ServeEvent) error {
		e.Router.GET("/api/archivist/tools", func(re *core.RequestEvent) error {
			if !re.HasSuperuserAuth() {
				return re.ForbiddenError("superusers only", nil)
			}
			job := LatestJob()
			var state map[string]any
			if job != nil {
				state = job.State()
			}
			return re.JSON(200, map[string]any{"tools": Descriptors(), "job": state})
		})
		e.Router.POST("/api/archivist/tools/{id}/run", func(re *core.RequestEvent) error {
			if !re.HasSuperuserAuth() {
				return re.ForbiddenError("superusers only", nil)
			}
			job, err := Start(re.App, s3, re.Request.PathValue("id"))
			if err != nil {
				return re.JSON(409, map[string]any{"message": err.Error()})
			}
			return re.JSON(200, job.State())
		})
		e.Router.GET("/api/archivist/tools/jobs/{id}", func(re *core.RequestEvent) error {
			if !re.HasSuperuserAuth() {
				return re.ForbiddenError("superusers only", nil)
			}
			job := JobByID(re.Request.PathValue("id"))
			if job == nil {
				return re.JSON(404, map[string]any{"message": "job not found"})
			}
			return re.JSON(200, job.State())
		})
		e.Router.POST("/api/archivist/tools/jobs/{id}/cancel", func(re *core.RequestEvent) error {
			if !re.HasSuperuserAuth() {
				return re.ForbiddenError("superusers only", nil)
			}
			if !Cancel(re.Request.PathValue("id")) {
				return re.JSON(404, map[string]any{"message": "no running job with this id"})
			}
			return re.JSON(200, map[string]any{"status": "cancelling"})
		})
		return e.Next()
	})
}
