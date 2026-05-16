import SwiftUI

struct ContentView: View {
    var body: some View {
        VStack(spacing: 24) {
            Image(systemName: "iphone.gen3")
                .font(.system(size: 80))
                .foregroundStyle(.blue)

            Text("Hello, World!")
                .font(.largeTitle)
                .fontWeight(.bold)

            Text("Running on iPhone 16 Pro Max")
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
        .padding()
    }
}

#Preview {
    ContentView()
}
