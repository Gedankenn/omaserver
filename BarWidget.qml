import QtQuick
import QtQuick.Effects
import Quickshell
import qs.Commons
import qs.Ui
import "Model.js" as Model

BarWidget {
  id: root
  moduleName: "sabinho.omaserver"

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
  }

  function refresh() {
    if (panelLoader.item && panelLoader.item.refresh) panelLoader.item.refresh()
  }

  function togglePanel() {
    if (panelLoader.item && panelLoader.item.toggle) panelLoader.item.toggle()
  }

  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false
  readonly property real openPanelIndicatorWidth: button.slotSize
  readonly property real openPanelIndicatorHeight: Math.max(Style.space(10), Math.round(Style.bar.iconSlot * 0.55))

  readonly property bool reachable: panelLoader.item ? panelLoader.item.reachable === true : false
  readonly property bool stressed: panelLoader.item ? panelLoader.item.stressed === true : false
  readonly property string tooltip: panelLoader.item ? panelLoader.item.tooltipText : "omaserver"

  readonly property color onColor: "#8FDB4F"
  readonly property color offColor: "#111111"
  readonly property color stressColor: "#E23B3B"
  readonly property color tintColor: !reachable ? offColor : (stressed ? stressColor : onColor)
  readonly property real tintAmount: !reachable || stressed ? 1 : 0

  function open() {
    if (panelLoader.item && panelLoader.item.openFromHotkey) panelLoader.item.openFromHotkey()
  }

  function close() {
    if (panelLoader.item && panelLoader.item.close) panelLoader.item.close()
  }

  function closeForPopoutSwitch() {
    if (panelLoader.item) panelLoader.item.closeForPopoutSwitch()
  }

  function notifyStatus() {
    if (!root.bar) return
    Util.execArgv(["omarchy-notification-send", "omaserver", root.tooltip])
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    tooltipText: Model.asPlainUi(root.tooltip, 240)
    slotSize: Style.bar.statusSlot
    iconComponent: omarchyMark

    onPressed: function(b) {
      if (b === Qt.RightButton) root.notifyStatus()
      else if (b === Qt.MiddleButton) root.refresh()
      else root.togglePanel()
    }
  }

  Component {
    id: omarchyMark
    Item {
      Image {
        id: mark
        anchors.fill: parent
        anchors.margins: 1
        fillMode: Image.PreserveAspectFit
        source: Qt.resolvedUrl("assets/omarchy.png")
        sourceSize.width: Math.round(Math.min(width, height) * Screen.devicePixelRatio)
        sourceSize.height: Math.round(Math.min(width, height) * Screen.devicePixelRatio)
        visible: false
        layer.enabled: true
      }

      MultiEffect {
        id: tinted
        anchors.fill: mark
        source: mark
        colorization: root.tintAmount
        colorizationColor: root.tintColor
        Behavior on colorization { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }
        Behavior on colorizationColor { ColorAnimation { duration: 180; easing.type: Easing.OutCubic } }

        SequentialAnimation on opacity {
          running: root.reachable && !root.stressed
          loops: Animation.Infinite
          NumberAnimation { from: 1.0; to: 0.72; duration: 900; easing.type: Easing.InOutQuad }
          NumberAnimation { from: 0.72; to: 1.0; duration: 900; easing.type: Easing.InOutQuad }
        }
      }
    }
  }
}
