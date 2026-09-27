import { app } from "../../scripts/app.js";

app.registerExtension({
  name: "DOGEMiningSession.Status",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "DOGEMiningSession") return;
    const previous = nodeType.prototype.onExecuted;
    nodeType.prototype.onExecuted = function (message) {
      previous?.apply(this, arguments);
      if (!this.dogeStatusWidget) {
        this.dogeStatusWidget = this.addWidget("text", "last_status", "", () => {}, { multiline: true, serialize: false });
      }
      this.dogeStatusWidget.value = (message.text || []).join("\n");
      this.setDirtyCanvas(true, true);
    };
  },
});
